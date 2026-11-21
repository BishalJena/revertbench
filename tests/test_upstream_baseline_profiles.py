"""Fixed upstream runtime selections retain actual unit and doctest evidence."""

import base64
import json
import zlib
from pathlib import Path
from typing import Any, cast

import docker
import pytest
from typer.testing import CliRunner

from revertbench.validate.baseline import (
    BaselineProfile,
    DockerBaselineRunner,
    baseline_command,
    parse_baseline_output,
)

BOLTONS = "boltons_upstream_default_behavior_v1"
PYDASH = "pydash_upstream_default_behavior_v1"
PROFILES = (BOLTONS, PYDASH)
PREFIX = (
    "/usr/local/bin/python",
    "-m",
    "pytest",
    "-q",
    "--disable-warnings",
    "-c",
    "/opt/revertbench/pytest.ini",
    "--rootdir",
    "/workspace/repo",
    "--basetemp=/tmp/revertbench-baseline",
    "-p",
    "no:cacheprovider",
)


def test_fixed_upstream_commands_and_legacy_defaults() -> None:
    assert baseline_command(cast(BaselineProfile, BOLTONS)) == (
        *PREFIX,
        "--doctest-modules",
        "-o",
        "doctest_optionflags=ALLOW_UNICODE ELLIPSIS IGNORE_EXCEPTION_DETAIL NORMALIZE_WHITESPACE",
        "boltons",
        "tests",
    )
    assert baseline_command(cast(BaselineProfile, PYDASH)) == (
        *PREFIX,
        "--doctest-modules",
        "--ignore=tests/pytest_mypy_testing",
        "src/pydash",
        "tests",
    )
    assert baseline_command("pytest") == (
        *PREFIX,
        "tests",
    )
    assert baseline_command("unittest") == ("/usr/local/bin/python", "-m", "unittest", "-q")


@pytest.mark.parametrize("value", ["pytest --ignore=tests", "boltons", True, [], None])
def test_unknown_profiles_cannot_supply_arguments(value: object) -> None:
    with pytest.raises(ValueError):
        baseline_command(cast(BaselineProfile, value))


@pytest.mark.parametrize("profile", PROFILES)
def test_new_pytest_profiles_preserve_large_node_vectors_and_reject_count_errors(
    profile: str,
) -> None:
    nodes = [f"src/library.py::test_example_{number}" for number in range(4000)]
    payload = dict(
        profile=profile,
        command=baseline_command(cast(BaselineProfile, profile)),
        collected=len(nodes),
        passed=len(nodes),
        node_ids=nodes,
    )
    raw = json.dumps(payload).encode()
    assert len(raw) > 65536

    def parse(data: dict[str, Any], code: int = 0) -> Any:
        packed = base64.b64encode(zlib.compress(json.dumps(data).encode()))
        return parse_baseline_output(
            b"REVERTBENCH_BASELINE_ZLIB=" + packed,
            b"",
            code,
            expected_profile=cast(BaselineProfile, profile),
        )

    result = parse(payload)
    assert result.status == "passed" and result.node_ids == tuple(nodes)
    assert parse({**payload, "passed": len(nodes) - 1}).reason == "baseline_report_inconsistent"
    assert parse({**payload, "errors": 1}, 1).status == "invalid_run"
    assert (
        parse({**payload, "command": ["python", "-m", "pytest", "tests"]}).reason
        == "baseline_command_mismatch"
    )


@pytest.mark.parametrize("profile", PROFILES)
def test_cli_selects_only_fixed_upstream_profile(
    profile: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from revertbench import cli

    calls: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> Path:
        calls.append(kwargs)
        return tmp_path / "case.json"

    monkeypatch.setattr(cli, "_process_candidate", capture)
    result = CliRunner().invoke(
        cli.app,
        [
            "validate",
            str(tmp_path / "candidate.json"),
            "--image",
            "sha256:" + "a" * 64,
            "--baseline-profile",
            profile,
            "--baseline-pids-limit",
            "256",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["baseline_profile"] == profile and calls[0]["baseline_pids_limit"] == 256


def _toy(tree: Path, package: str) -> Path:
    source = tree / ("src/pydash" if package == "pydash" else "boltons")
    source.mkdir(parents=True)
    (source / "__init__.py").write_text('"""A runtime example.\n>>> 2 + 2\n5\n"""\n')
    tests = tree / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text(
        "import pytest\n@pytest.fixture\ndef original_fixture(): return 7\n"
    )
    (tests / "test_unit.py").write_text(
        "def test_unit(original_fixture): assert original_fixture == 7\n"
    )
    return source


@pytest.mark.docker
def test_boltons_profile_runs_doctests_and_explicit_flags(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    source = _toy(tmp_path, "boltons")
    legacy = DockerBaselineRunner(worker_image, client=docker_client)(tmp_path)
    assert legacy.status == "passed" and legacy.passed == 1
    runner = DockerBaselineRunner(
        worker_image, client=docker_client, profile=cast(BaselineProfile, BOLTONS)
    )
    broken = runner(tmp_path)
    assert broken.status == "failed" and broken.failed == 1 and broken.passed == 1
    (source / "__init__.py").write_text(
        '"""Options example.\n>>> print("alpha  beta gamma")\nalpha beta ...\n"""\n'
    )
    good = runner(tmp_path)
    assert good.status == "passed" and good.passed == good.collected == 2, good.model_dump_json()
    assert any("boltons/__init__.py" in node for node in good.node_ids)
    assert good.command == baseline_command(cast(BaselineProfile, BOLTONS))


@pytest.mark.docker
def test_pydash_profile_keeps_default_typing_exclusion_and_other_failures(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    source = _toy(tmp_path, "pydash")
    (source / "__init__.py").write_text('"""Example.\n>>> 2 + 2\n4\n"""\n')
    typing = tmp_path / "tests/pytest_mypy_testing"
    typing.mkdir()
    (typing / "test_opt_in.py").write_text('raise ImportError("opt-in typing plugin required")\n')
    runner = DockerBaselineRunner(
        worker_image, client=docker_client, profile=cast(BaselineProfile, PYDASH)
    )
    good = runner(tmp_path)
    assert good.status == "passed" and good.passed == good.collected == 2, good.model_dump_json()
    assert not any("pytest_mypy_testing" in node for node in good.node_ids)
    (tmp_path / "tests/test_other.py").write_text("def test_other(): assert False\n")
    failed = runner(tmp_path)
    assert failed.status == "failed" and failed.failed == 1 and failed.passed == 2
    (tmp_path / "tests/test_other.py").write_text(
        'raise ImportError("required support unavailable")\n'
    )
    broken = runner(tmp_path)
    # --doctest-modules has separate module and doctest collection reports.
    assert broken.status == "invalid_run" and broken.errors == 2


@pytest.mark.parametrize(
    "field,value",
    [("command", ("python", "-m", "pytest", "tests")), ("profile", "pytest"), ("pids_limit", 64)],
)
def test_new_profile_backend_mismatch_cannot_machine_validate(
    tmp_path: Path, field: str, value: object
) -> None:
    from revertbench.validate import validate_built_candidate
    from revertbench.validate.baseline import BaselineResult
    from test_case_validation import _built_toy, _passing_baseline

    seen = 0

    def backend(tree: Path) -> BaselineResult:
        nonlocal seen
        seen += 1
        updates: dict[str, Any] = dict(
            profile=BOLTONS,
            command=baseline_command(cast(BaselineProfile, BOLTONS)),
            pids_limit=256,
        )
        if seen == 2:
            updates[field] = value
        return _passing_baseline(tree).model_copy(update=updates)

    report = validate_built_candidate(
        _built_toy(tmp_path),
        image="sha256:" + "a" * 64,
        baseline_profile=cast(BaselineProfile, BOLTONS),
        baseline_pids_limit=256,
        baseline_backend=backend,
    )
    assert report.status == "pending_infrastructure"
    assert report.reason == (
        "baseline_resource_mismatch" if field == "pids_limit" else "baseline_profile_mismatch"
    )
    assert report.reference is None


@pytest.mark.parametrize("tamper", ["top_command", "role_command", "role_profile", "missing_role"])
def test_validated_new_profile_report_binds_exact_declared_selection(tamper: str) -> None:
    from pydantic import ValidationError

    from revertbench.validate import MachineValidationReport

    command = baseline_command(cast(BaselineProfile, PYDASH))
    role = dict(status="passed", profile=PYDASH, command=list(command))
    data: dict[str, Any] = dict(
        status="machine_validated",
        case_id="toy",
        reason="passed",
        image="sha256:" + "a" * 64,
        source_hashes={},
        patch_sha256="b" * 64,
        reference_sha256="c" * 64,
        baseline_profile=PYDASH,
        baseline_command=list(command),
        baseline_reports={name: dict(role) for name in ("base", "bad", "repaired")},
    )
    assert MachineValidationReport.model_validate_json(json.dumps(data)).baseline_profile == PYDASH
    if tamper == "top_command":
        data["baseline_command"] = ["python", "-m", "pytest", "tests"]
    elif tamper == "missing_role":
        del data["baseline_reports"]["bad"]
    else:
        key = "command" if tamper == "role_command" else "profile"
        data["baseline_reports"]["bad"][key] = ["pytest", "tests"] if key == "command" else "pytest"
    with pytest.raises(ValidationError):
        MachineValidationReport.model_validate_json(json.dumps(data))
