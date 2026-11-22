"""Fixed upstream runtime selections retain actual unit and doctest evidence."""

import base64
import hashlib
import json
import os
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any, Final, cast

import docker
import pytest
from typer.testing import CliRunner

from revertbench.sandbox.worker import SandboxLimits
from revertbench.validate.baseline import (
    BaselineProfile,
    BaselineResult,
    DockerBaselineRunner,
    _pytest_script,
    baseline_command,
    parse_baseline_output,
)

BOLTONS = "boltons_upstream_default_behavior_v1"
PYDASH = "pydash_upstream_default_behavior_v1"
BLINKER = "blinker_upstream_default_behavior_v1"
BABEL: Final = "babel_upstream_default_behavior_v1"
BABEL_DATA_SHA256 = "38c999568bebecfc77cb41875776beb97382dd55b3133aaaac73f6dd98c6cf5e"
BABEL_COLLECTION_SKIPS = (
    "tests/interop/test_jinja2_interop.py",
    "tests/messages/test_setuptools_frontend.py",
)
PROFILES = (BOLTONS, PYDASH, BLINKER)
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


def test_blinker_command_preserves_native_async_and_warning_contract() -> None:
    assert baseline_command(cast(BaselineProfile, BLINKER)) == (
        "/usr/local/bin/python",
        "-m",
        "pytest",
        "-v",
        "--tb=short",
        "-c",
        "/opt/revertbench/pytest.ini",
        "--rootdir",
        "/workspace/repo",
        "--basetemp=/tmp/revertbench-baseline",
        "-p",
        "no:cacheprovider",
        "-p",
        "pytest_asyncio.plugin",
        "-o",
        "filterwarnings=error",
        "-o",
        "asyncio_mode=auto",
        "-o",
        "asyncio_default_fixture_loop_scope=function",
        "tests",
    )


def test_babel_command_preserves_native_root_collection() -> None:
    assert baseline_command(BABEL) == (
        "/usr/local/bin/python",
        "-m",
        "pytest",
        "-q",
        "--tb=short",
        "-c",
        "/workspace/repo/setup.cfg",
        "--rootdir",
        "/workspace/repo",
        "--basetemp=/tmp/revertbench-baseline",
        "-p",
        "no:cacheprovider",
        ".",
    )


def test_babel_fixes_native_baseline_memory_and_preserves_other_runner_limits() -> None:
    image = "sha256:" + "a" * 64
    limits = SandboxLimits(pids=96, cpus=2.0)
    legacy = DockerBaselineRunner(image, limits=limits)
    babel = DockerBaselineRunner(image, limits=limits, profile=BABEL)
    assert legacy.limits == limits
    assert babel.limits == SandboxLimits(memory_bytes=512 * 1024**2, pids=96, cpus=2.0)
    assert DockerBaselineRunner(image, profile=BABEL).limits == SandboxLimits(
        memory_bytes=512 * 1024**2
    )


@pytest.mark.parametrize(
    "extras,skipped,status",
    [
        (BABEL_COLLECTION_SKIPS, 2, "passed"),
        (BABEL_COLLECTION_SKIPS, 3, "invalid_run"),
        (BABEL_COLLECTION_SKIPS[:1], 1, "invalid_run"),
        ((BABEL_COLLECTION_SKIPS[0],) * 2, 2, "invalid_run"),
        ((*BABEL_COLLECTION_SKIPS, "tests/test_other.py"), 3, "invalid_run"),
    ],
)
def test_babel_report_accepts_only_the_two_native_collection_skips(
    extras: tuple[str, ...], skipped: int, status: str
) -> None:
    data = {
        "profile": BABEL,
        "command": baseline_command(BABEL),
        "collected": 1,
        "passed": 1,
        "skipped": skipped,
        "node_ids": ["babel/numbers.py::babel.numbers.format_decimal"],
        "extra_skip_node_ids": extras,
    }
    packed = base64.b64encode(zlib.compress(json.dumps(data).encode()))
    result = parse_baseline_output(
        b"REVERTBENCH_BASELINE_ZLIB=" + packed, b"", 0, expected_profile=BABEL
    )
    assert result.status == status


def _babel_fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path / "repo"
    package = repo / "babel"
    (package / "locale-data").mkdir(parents=True)
    (package / "__init__.py").write_text(
        '"""Original doctest.\n>>> print("native  flags enabled")\nnative flags enabled\n"""\n'
        "from pathlib import Path\n"
        "assert Path(__file__).with_name('global.dat').read_bytes() == b'toy globals'\n"
    )
    (repo / "setup.cfg").write_text("[tool:pytest]\ndoctest_optionflags = NORMALIZE_WHITESPACE\n")
    (repo / "pytest.ini").write_text("[pytest]\naddopts = --ignore=tests\n")
    (repo / "conftest.py").write_text(
        "from _pytest.doctest import DoctestModule\n"
        "def pytest_collect_file(file_path, parent):\n"
        "    if file_path.name == '__init__.py' and file_path.parent.name == 'babel':\n"
        "        return DoctestModule.from_parent(parent, path=file_path)\n"
    )
    for path, dependency in zip(BABEL_COLLECTION_SKIPS, ("jinja2", "setuptools"), strict=True):
        module = repo / path
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text(
            "import pytest\n"
            f"pytest.skip(\"could not import '{dependency}': No module named '{dependency}'\", "
            "allow_module_level=True)\n"
        )
    (repo / "tests/test_localedata.py").write_text(
        "import pytest\n"
        "@pytest.mark.skip(reason='windows-only test')\n"
        "def test_reserved_locale_names(): pass\n"
    )
    (repo / "tests/test_example.py").write_text(
        "def test_native_unit():\n"
        "    from pathlib import Path\n"
        "    import babel\n"
        "    data = Path(babel.__file__).with_name('locale-data') / 'root.dat'\n"
        "    assert data.read_bytes() == b'toy locale'\n"
    )
    overlay = tmp_path / "overlay"
    data = overlay / "data/babel"
    (data / "locale-data").mkdir(parents=True)
    deps = overlay / "deps"
    for name, version in (("freezegun", "1.5.1"), ("python_dateutil", "2.9.0.post0")):
        metadata = deps / f"{name}-{version}.dist-info"
        metadata.mkdir(parents=True)
        (metadata / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
    files = {"global.dat": b"toy globals", "locale-data/root.dat": b"toy locale"}
    manifest = hashlib.sha256()
    for relative, content in files.items():
        (data / relative).write_bytes(content)
        manifest.update(f"babel/{relative} {hashlib.sha256(content).hexdigest()}\n".encode())
    return repo, overlay, manifest.hexdigest()


def _run_babel_fixture(
    tmp_path: Path, repo: Path, overlay: Path, data_digest: str
) -> subprocess.CompletedProcess[bytes]:
    trusted = tmp_path / "trusted"
    (trusted / "src/revertbench/grade").mkdir(parents=True)
    (trusted / "src/revertbench/grade/policy.py").touch()
    (trusted / "uv.lock").touch()
    script = (
        _pytest_script(BABEL)
        .replace(BABEL_DATA_SHA256, data_digest)
        .replace("/opt/revertbench/babel", str(overlay))
        .replace("/opt/revertbench", str(trusted))
        .replace("/workspace/repo", str(repo))
        .replace("/tmp/revertbench-baseline", str(tmp_path / "pytest-basetemp"))
    )
    return subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=repo,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""},
        capture_output=True,
        timeout=15,
        check=False,
    )


def test_babel_prepares_frozen_data_and_keeps_native_doctests_and_skips(tmp_path: Path) -> None:
    repo, overlay, digest = _babel_fixture(tmp_path)
    run = _run_babel_fixture(tmp_path, repo, overlay, digest)
    assert run.returncode == 0, (run.stdout, run.stderr)
    result = _parse_babel_fixture(run)
    assert result.status == "passed", result.model_dump_json()
    assert result.passed == 2 and result.collected == result.skipped == 3
    assert set(result.node_ids) == {
        "babel/__init__.py::babel",
        "tests/test_example.py::test_native_unit",
        "tests/test_localedata.py::test_reserved_locale_names",
    }
    assert result.extra_skip_node_ids == BABEL_COLLECTION_SKIPS


def _parse_babel_fixture(run: subprocess.CompletedProcess[bytes]) -> BaselineResult:
    packed = next(
        line.split(b"=", 1)[1]
        for line in run.stdout.splitlines()
        if line.startswith(b"REVERTBENCH_BASELINE_ZLIB=")
    )
    payload = json.loads(zlib.decompress(base64.b64decode(packed)))
    payload["command"] = baseline_command(BABEL)
    return parse_baseline_output(
        b"REVERTBENCH_BASELINE=" + json.dumps(payload).encode(),
        run.stderr,
        run.returncode,
        expected_profile=BABEL,
    )


@pytest.mark.parametrize(
    "change", ("digest", "symlink", "collision", "extra_locale", "overlay_shape", "deps")
)
def test_babel_preflight_rejects_bad_data_before_importing_repo(
    tmp_path: Path, change: str
) -> None:
    repo, overlay, digest = _babel_fixture(tmp_path)
    (repo / "conftest.py").write_text("from pathlib import Path\nPath('imported').touch()\n")
    data = overlay / "data/babel"
    if change == "digest":
        (data / "global.dat").write_bytes(b"wrong data")
    elif change == "symlink":
        (data / "global.dat").rename(overlay / "real.dat")
        (data / "global.dat").symlink_to(overlay / "real.dat")
    elif change == "collision":
        (repo / "babel/global.dat").symlink_to(repo / "missing.dat")
    elif change == "extra_locale":
        (repo / "babel/locale-data/extra.dat").touch()
    elif change == "overlay_shape":
        (data / "locale-data/extraneous").touch()
    else:
        (overlay / "deps/freezegun-1.5.1.dist-info/METADATA").write_text(
            "Name: freezegun\nVersion: 2.0\n"
        )
    run = _run_babel_fixture(tmp_path, repo, overlay, digest)
    assert run.returncode == 4, (run.stdout, run.stderr)
    assert not (repo / "imported").exists()
    assert not (repo / "babel/locale-data/root.dat").exists()


@pytest.mark.parametrize("collection", (True, False))
def test_babel_rejects_unexpected_native_skip(tmp_path: Path, collection: bool) -> None:
    repo, overlay, digest = _babel_fixture(tmp_path)
    skip = (
        "pytest.skip('unexpected suite skip', allow_module_level=True)"
        if collection
        else "def test_unexpected(): pytest.skip('pytz not available')"
    )
    (repo / "tests/test_unexpected.py").write_text(f"import pytest\n{skip}\n")
    run = _run_babel_fixture(tmp_path, repo, overlay, digest)
    result = _parse_babel_fixture(run)
    assert result.status == "invalid_run"
    assert any("unexpected Babel skip" in detail for detail in result.details)


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


@pytest.mark.parametrize("profile", (*PROFILES, BABEL))
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


def _async_toy(tree: Path) -> None:
    (tree / "tests").mkdir()
    # Fixed profiles must not inherit an unrelated repository's extra selection.
    (tree / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts="--ignore=tests/test_unit.py"\n'
    )
    (tree / "tests/test_unit.py").write_text(
        "import asyncio\n"
        "def test_sync(): assert 2 + 3 == 5\n"
        "async def test_async():\n"
        "    await asyncio.sleep(0)\n"
        "    assert 2 + 3 == 5\n"
    )


@pytest.mark.docker
def test_blinker_profile_executes_native_sync_and_async_with_explicit_plugin(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    _async_toy(tmp_path)
    result = DockerBaselineRunner(
        worker_image, client=docker_client, profile=cast(BaselineProfile, BLINKER)
    )(tmp_path)
    assert result.status == "passed", result.model_dump_json()
    assert result.collected == result.passed == 2
    assert result.skipped == result.xfailed == result.xpassed == result.deselected == 0
    assert set(result.node_ids) == {
        "tests/test_unit.py::test_sync",
        "tests/test_unit.py::test_async",
    }
    assert result.command == baseline_command(cast(BaselineProfile, BLINKER))


@pytest.mark.docker
def test_blinker_profile_preserves_warning_errors_instead_of_filtering_them(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    _async_toy(tmp_path)
    (tmp_path / "tests/test_warning.py").write_text(
        "import warnings\n"
        "def test_original_warning():\n"
        "    warnings.warn('native warning', DeprecationWarning)\n"
    )
    result = DockerBaselineRunner(
        worker_image, client=docker_client, profile=cast(BaselineProfile, BLINKER)
    )(tmp_path)
    assert result.status == "failed" and result.reason == "existing_suite_failed"
    assert result.collected == 3 and result.passed == 2 and result.failed == 1
    assert result.skipped == result.deselected == 0


@pytest.mark.docker
def test_blinker_profile_rejects_repository_shadowing_of_allowed_async_plugin(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    _async_toy(tmp_path)
    (tmp_path / "tests/test_unit.py").write_text("def test_sync(): assert 2 + 3 == 5\n")
    package = tmp_path / "pytest_asyncio"
    package.mkdir()
    (package / "__init__.py").write_text("")
    # A harmless local package must not replace the trusted installed plugin.
    (package / "plugin.py").write_text(
        "LOCAL_MARKER = True\n"
        "def pytest_addoption(parser):\n"
        "    parser.addini('asyncio_mode', 'mode', default='auto')\n"
        "    parser.addini('asyncio_default_fixture_loop_scope', 'scope', default='function')\n"
    )
    result = DockerBaselineRunner(
        worker_image, client=docker_client, profile=cast(BaselineProfile, BLINKER)
    )(tmp_path)
    assert result.status == "invalid_run", result.model_dump_json()
    assert result.reason == "baseline_collection_or_worker_error"


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
