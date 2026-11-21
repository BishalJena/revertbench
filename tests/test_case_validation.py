"""Machine validation distinguishes real case rejection from missing infrastructure."""

import json
from pathlib import Path
from typing import Any

import docker
import pytest

from revertbench.build import BuiltCandidate
from revertbench.validate.baseline import BaselineResult, parse_baseline_output
from revertbench.validate.identity import host_code_identity


def _passing_baseline(tree: Path) -> BaselineResult:
    from revertbench.validate.baseline import baseline_command

    grader, policy = host_code_identity()
    return BaselineResult(
        status="passed",
        command=baseline_command("pytest"),
        collected=1,
        passed=1,
        environment_python="3.12.15",
        dependency_lock_sha256="b" * 64,
        grader_sha256=grader,
        policy_sha256=policy,
    )


def test_baseline_reports_maintainer_xfail_xpass_separately() -> None:
    payload = {
        "collected": 540,
        "passed": 537,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "xfailed": 2,
        "xpassed": 1,
        "deselected": 0,
        "node_ids": [f"test_{i}" for i in range(540)],
        "details": [],
    }
    result = parse_baseline_output(
        b"progress\nREVERTBENCH_BASELINE=" + json.dumps(payload).encode(), b"", 0
    )
    assert result.status == "passed"
    assert result.passed == 537 and result.xfailed == 2 and result.xpassed == 1


@pytest.mark.parametrize("collected,passed,skipped,exit_code", [(0, 0, 0, 5), (3, 0, 3, 0)])
def test_zero_or_all_skipped_baseline_is_not_a_case_pass(
    collected: int, passed: int, skipped: int, exit_code: int
) -> None:
    payload = {
        "collected": collected,
        "passed": passed,
        "skipped": skipped,
        "node_ids": [f"test_{i}" for i in range(collected)],
    }
    result = parse_baseline_output(
        b"REVERTBENCH_BASELINE=" + json.dumps(payload).encode(), b"", exit_code
    )
    assert result.status == "failed"
    assert result.reason == "no_executed_tests"


def test_baseline_collection_dependency_failure_is_pending_infrastructure() -> None:
    payload = {"collected": 0, "passed": 0, "errors": 1, "details": ["ImportError"]}
    result = parse_baseline_output(b"REVERTBENCH_BASELINE=" + json.dumps(payload).encode(), b"", 2)
    assert result.status == "invalid_run"


def test_real_baseline_assertion_failure_rejects_case() -> None:
    payload = {"collected": 2, "passed": 1, "failed": 1, "node_ids": ["test_a", "test_b"]}
    result = parse_baseline_output(b"REVERTBENCH_BASELINE=" + json.dumps(payload).encode(), b"", 1)
    assert result.status == "failed"
    assert result.reason == "existing_suite_failed"


def test_lost_or_malformed_baseline_report_is_not_a_pass() -> None:
    for data in (b"", b"REVERTBENCH_BASELINE=not JSON", b"REVERTBENCH_BASELINE=" + b"[" * 10000):
        assert parse_baseline_output(data, b"", 0).status == "invalid_run"


def test_baseline_counts_are_strict() -> None:
    with pytest.raises(ValueError):
        BaselineResult(status="passed", collected=1, passed=True)


def test_machine_validation_rejects_unsafe_reference_before_container(tmp_path: Path) -> None:
    from dataclasses import replace

    from revertbench.build import materialize_candidate
    from revertbench.validate import validate_built_candidate
    from test_build_candidate import _candidate, _Repository

    built = materialize_candidate(_Repository(), _candidate(), tmp_path / "build")
    built = replace(
        built, reference_source="import os\ndef test_regression():\n assert os.getcwd()\n"
    )

    def unavailable(tree: Path) -> BaselineResult:
        pytest.fail("unsafe reference must not start a container")

    report = validate_built_candidate(
        built, image="sha256:" + "a" * 64, baseline_backend=unavailable
    )
    assert report.status == "rejected"
    assert report.reason == "reference_policy_rejected"
    assert report.human_review == "pending"


def test_baseline_infrastructure_is_not_candidate_rejection(tmp_path: Path) -> None:
    from revertbench.build import materialize_candidate
    from revertbench.validate import validate_built_candidate
    from test_build_candidate import _candidate, _Repository

    built = materialize_candidate(_Repository(), _candidate(), tmp_path / "build")
    report = validate_built_candidate(
        built,
        image="sha256:" + "a" * 64,
        baseline_backend=lambda tree: BaselineResult(
            status="invalid_run", reason="daemon_unavailable"
        ),
    )
    assert report.status == "pending_infrastructure"
    assert report.reason == "baseline_infrastructure"


def _built_toy(tmp_path: Path) -> BuiltCandidate:
    from revertbench.build import canonical_tree_digest, materialize_candidate
    from test_build_candidate import _candidate, _Repository

    built = materialize_candidate(_Repository(), _candidate(), tmp_path / "build")
    (built.base_tree / "toy.py").write_text(
        "def normalize(value):\n    return value.strip().lower()\n"
    )
    for tree in (built.base_tree, built.bad_tree, built.repaired_tree, built.historical_fix_tree):
        (tree / "tests").mkdir(exist_ok=True)
        (tree / "tests/test_existing.py").write_text(
            "from toy import normalize\ndef test_existing():\n assert normalize(' x ') == 'x'\n"
        )
    from dataclasses import replace

    return replace(
        built,
        source_hashes={
            name: canonical_tree_digest(tree)
            for name, tree in [
                ("base", built.base_tree),
                ("bad", built.bad_tree),
                ("repaired", built.repaired_tree),
                ("historical_fix", built.historical_fix_tree),
            ]
        },
    )


def test_complete_machine_probes_remain_pending_human_review(tmp_path: Path) -> None:
    from revertbench.grade.runner import run_tree
    from revertbench.validate import validate_built_candidate

    built = _built_toy(tmp_path)
    report = validate_built_candidate(
        built,
        image="sha256:" + "a" * 64,
        baseline_backend=_passing_baseline,
        execution_backend=run_tree,
        transfer_backend=lambda tree, source: source,
    )
    assert report.status == "machine_validated", report.model_dump_json()
    assert report.human_review == "pending"
    assert report.reference is not None and len(report.reference.executions) == 9
    assert set(report.probes) == {
        "empty",
        "assert_true",
        "assert_false",
        "import_error",
        "fix_only",
        "stability",
        "config_tamper",
    }
    assert report.probes["fix_only"].status == "not_applicable"
    assert report.probes["fix_only"].reason == "no_added_symbols"
    assert report.probes["assert_false"].grade is not None
    assert report.probes["assert_false"].grade.score == 0


def test_machine_probe_infrastructure_cannot_be_a_successful_cheat_block(tmp_path: Path) -> None:
    from revertbench.grade.runner import run_tree
    from revertbench.schema import ExecutionResult
    from revertbench.validate import validate_built_candidate

    built = _built_toy(tmp_path)

    def execute(
        tree: Path,
        submission: str,
        *,
        seed: int,
        timeout_seconds: float = 10,
        output_limit_bytes: int = 65536,
    ) -> ExecutionResult:
        if "assert False" in submission:
            return ExecutionResult(status="invalid_run", seed=seed, reason="daemon lost")
        return run_tree(
            tree,
            submission,
            seed=seed,
            timeout_seconds=timeout_seconds,
            output_limit_bytes=output_limit_bytes,
        )

    report = validate_built_candidate(
        built,
        image="sha256:" + "a" * 64,
        baseline_backend=_passing_baseline,
        execution_backend=execute,
        transfer_backend=lambda tree, source: source,
    )
    assert report.status == "pending_infrastructure"
    assert report.probes["assert_false"].status == "invalid"


@pytest.mark.docker
def test_real_container_machine_validation_and_original_suite(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    from revertbench.validate import validate_built_candidate

    built = _built_toy(tmp_path)
    report = validate_built_candidate(built, image=worker_image, client=docker_client)
    assert report.status == "machine_validated", report.model_dump_json()
    assert all(item.passed == 1 for item in report.baseline_reports.values())
    assert report.environment_python is not None and report.environment_python.startswith("3.12.")
    assert report.dependency_lock_sha256 is not None and len(report.dependency_lock_sha256) == 64
    assert report.grader_sha256 is not None and len(report.grader_sha256) == 64
    assert report.policy_sha256 is not None and len(report.policy_sha256) == 64
    assert report.human_review == "pending"


@pytest.mark.parametrize(
    "payload",
    [
        {
            "collected": 2,
            "passed": 1,
            "node_ids": ["tests/test_a.py::test_a", "tests/test_b.py::test_b"],
        },
        {"collected": 1, "passed": 2, "node_ids": ["tests/test_a.py::test_a"]},
        {"collected": 2, "passed": 2, "node_ids": ["same", "same"]},
    ],
)
def test_incomplete_or_contradictory_baseline_evidence_is_invalid(
    payload: dict[str, object],
) -> None:
    result = parse_baseline_output(b"REVERTBENCH_BASELINE=" + json.dumps(payload).encode(), b"", 0)
    assert result.status == "invalid_run"


def test_existing_suite_failure_on_controlled_repair_rejects_before_reference(
    tmp_path: Path,
) -> None:
    from revertbench.validate import validate_built_candidate

    built = _built_toy(tmp_path)
    report = validate_built_candidate(
        built,
        image="sha256:" + "a" * 64,
        baseline_backend=lambda tree: BaselineResult(
            status="failed" if tree == built.repaired_tree else "passed", collected=1, passed=1
        ),
    )
    assert report.status == "rejected"
    assert report.reference is None
    assert report.reason == "existing_suite_failed"


def test_machine_report_preserves_observed_environment_and_code_identity(tmp_path: Path) -> None:
    from revertbench.grade.runner import run_tree
    from revertbench.validate import validate_built_candidate

    built = _built_toy(tmp_path)
    report = validate_built_candidate(
        built,
        image="sha256:" + "a" * 64,
        baseline_backend=_passing_baseline,
        execution_backend=run_tree,
        transfer_backend=lambda tree, source: source,
    )
    assert report.status == "machine_validated"
    assert report.environment_python == "3.12.15"
    assert report.dependency_lock_sha256 == "b" * 64
    assert (report.grader_sha256, report.policy_sha256) == host_code_identity()


def test_python_syntax_incompatibility_is_an_unsupported_candidate() -> None:
    payload = {
        "collected": 0,
        "passed": 0,
        "errors": 1,
        "details": ["SyntaxError: Missing parentheses in call to print"],
    }
    result = parse_baseline_output(b"REVERTBENCH_BASELINE=" + json.dumps(payload).encode(), b"", 2)
    assert result.status == "failed"
    assert result.reason == "unsupported_environment"


def test_exit_no_tests_with_pass_counts_is_contradictory() -> None:
    payload = {"collected": 1, "passed": 1, "node_ids": ["test_a"]}
    assert (
        parse_baseline_output(
            b"REVERTBENCH_BASELINE=" + json.dumps(payload).encode(), b"", 5
        ).status
        == "invalid_run"
    )


def test_fix_symbol_inventory_rejects_raw_non_utf8_codec_without_lookup(tmp_path: Path) -> None:
    import codecs

    from revertbench.build import BuildError
    from revertbench.validate.controller import _symbols

    calls: list[str] = []

    def search(name: str) -> None:
        calls.append(name)
        return None

    path = tmp_path / "toy.py"
    path.write_text("# coding: revertbench_unknown_codec\ndef new_symbol():\n    return 1\n")
    codecs.register(search)
    try:
        with pytest.raises(BuildError, match="unsupported_production_source"):
            _symbols(path)
    finally:
        codecs.unregister(search)
    assert calls == []


@pytest.mark.parametrize(
    "outcome,xfail,missing",
    [("skipped", False, False), ("passed", True, False), ("passed", False, True)],
)
def test_history_probe_does_not_call_incomplete_or_skipped_reference_a_pass(
    tmp_path: Path, outcome: str, xfail: bool, missing: bool
) -> None:
    from revertbench.schema import ExecutionResult, PhaseResult, TestResult
    from revertbench.validate import DockerReferenceProbe

    (tmp_path / "toy.py").write_text("def normalize(value):\n return value\n")
    phase = PhaseResult(outcome="passed")
    call = PhaseResult.model_validate({"outcome": outcome, "was_xfail": xfail})
    result = ExecutionResult(
        status="completed",
        seed=11,
        exit_code=0,
        collected_node_ids=("test_submission.py::test_ok",),
        execution_order=("test_submission.py::test_ok",),
        tests=(
            TestResult(
                node_id="test_submission.py::test_ok",
                setup=phase,
                call=None if missing else call,
                teardown=phase,
            ),
        ),
    )

    def execute(
        tree: Path,
        submission: str,
        *,
        seed: int,
        timeout_seconds: float = 10,
        output_limit_bytes: int = 65536,
    ) -> ExecutionResult:
        return result

    probe = DockerReferenceProbe("sha256:" + "a" * 64, execution_backend=execute)
    assert (
        probe.classify(
            tmp_path, "from toy import normalize\ndef test_ok():\n assert normalize('x')=='x'\n"
        )
        == "inconclusive"
    )


def test_fix_only_symbol_probe_includes_added_class_methods(tmp_path: Path) -> None:
    from revertbench.validate.controller import _fix_only_source

    built = _built_toy(tmp_path)
    (built.bad_tree / "toy.py").write_text("class Parser:\n    pass\n")
    (built.repaired_tree / "toy.py").write_text(
        'class Parser:\n    def normalize(self):\n        return "x"\n'
    )
    source = _fix_only_source(built)
    assert source is not None
    assert "Parser.normalize" in source


@pytest.mark.parametrize(
    "exception,trace,expected",
    [
        ("IndexError", ("toy.py",), "fail"),
        ("TypeError", ("toy.py",), "fail"),
        ("NameError", ("toy.py",), "inconclusive"),
        ("AttributeError", ("toy.py",), "inconclusive"),
        ("pretend.IndexError", ("toy.py",), "inconclusive"),
        ("IndexError", ("external:pytest.py",), "inconclusive"),
    ],
)
def test_history_probe_uses_supported_exception_and_own_project_provenance(
    tmp_path: Path, exception: str, trace: tuple[str, ...], expected: str
) -> None:
    from revertbench.schema import ExecutionResult, PhaseResult, TestResult
    from revertbench.validate import DockerReferenceProbe

    (tmp_path / "toy.py").write_text("def normalize(value):\n return value\n")
    phase = PhaseResult(outcome="passed")
    result = ExecutionResult(
        status="completed",
        seed=11,
        exit_code=1,
        collected_node_ids=("test_submission.py::test_ok",),
        execution_order=("test_submission.py::test_ok",),
        tests=(
            TestResult(
                node_id="test_submission.py::test_ok",
                setup=phase,
                call=PhaseResult(outcome="failed", exception_type=exception, traceback_paths=trace),
                teardown=phase,
                covered_lines={"toy.py": (2,)},
            ),
        ),
    )

    def execute(
        tree: Path,
        submission: str,
        *,
        seed: int,
        timeout_seconds: float = 10,
        output_limit_bytes: int = 65536,
    ) -> ExecutionResult:
        return result

    probe = DockerReferenceProbe("sha256:" + "a" * 64, execution_backend=execute)
    assert (
        probe.classify(
            tmp_path, "from toy import normalize\ndef test_ok():\n assert normalize('x')=='x'\n"
        )
        == expected
    )


@pytest.mark.parametrize(
    "missing", ["environment_python", "dependency_lock_sha256", "grader_sha256", "policy_sha256"]
)
def test_machine_validation_requires_observed_environment_identity(
    tmp_path: Path, missing: str
) -> None:
    from revertbench.validate import validate_built_candidate

    built = _built_toy(tmp_path)
    report = validate_built_candidate(
        built,
        image="sha256:" + "a" * 64,
        baseline_backend=lambda tree: _passing_baseline(tree).model_copy(update={missing: None}),
    )
    assert report.status == "pending_infrastructure"
    assert report.reason == "environment_identity_missing"
    assert report.reference is None


def test_machine_validation_rejects_consistently_stale_worker_code(tmp_path: Path) -> None:
    from revertbench.validate import validate_built_candidate

    built = _built_toy(tmp_path)
    report = validate_built_candidate(
        built,
        image="sha256:" + "a" * 64,
        baseline_backend=lambda tree: BaselineResult(
            status="passed",
            collected=1,
            passed=1,
            environment_python="3.12.16",
            dependency_lock_sha256="a" * 64,
            grader_sha256="b" * 64,
            policy_sha256="c" * 64,
        ),
    )
    assert report.status == "pending_infrastructure"
    assert report.reason == "worker_code_mismatch"
    assert report.reference is None


def test_code_identity_is_location_independent_and_tracks_python_sources(tmp_path: Path) -> None:
    from revertbench.validate.identity import package_identity

    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        (root / "grade").mkdir(parents=True)
        (root / "grade/policy.py").write_text('POLICY_VERSION = "2"\n')
        (root / "api.py").write_text("VALUE = 1\n")
    original = package_identity(roots[0])
    assert package_identity(roots[1]) == original
    (roots[1] / "notes.txt").write_text("not an installed Python source")
    assert package_identity(roots[1]) == original
    (roots[1] / "api.py").rename(roots[1] / "renamed.py")
    renamed = package_identity(roots[1])
    assert renamed[0] != original[0] and renamed[1] == original[1]
    (roots[1] / "grade/policy.py").write_text('POLICY_VERSION = "3"\n')
    assert all(new != old for new, old in zip(package_identity(roots[1]), renamed, strict=True))


def test_explicit_unittest_profile_is_preserved_and_matches_backend(tmp_path: Path) -> None:
    from revertbench.grade.runner import run_tree
    from revertbench.validate import validate_built_candidate
    from revertbench.validate.baseline import baseline_command

    built = _built_toy(tmp_path)

    def baseline(tree: Path) -> BaselineResult:
        return _passing_baseline(tree).model_copy(
            update={"profile": "unittest", "command": baseline_command("unittest")}
        )

    report = validate_built_candidate(
        built,
        image="sha256:" + "a" * 64,
        baseline_profile="unittest",
        baseline_backend=baseline,
        execution_backend=run_tree,
        transfer_backend=lambda tree, source: source,
    )
    assert report.status == "machine_validated", report.model_dump_json()
    assert report.baseline_profile == "unittest"
    assert report.baseline_command == ("/usr/local/bin/python", "-m", "unittest", "-q")


def test_backend_cannot_silently_run_a_different_baseline_profile(tmp_path: Path) -> None:
    from revertbench.validate import validate_built_candidate

    report = validate_built_candidate(
        _built_toy(tmp_path),
        image="sha256:" + "a" * 64,
        baseline_profile="unittest",
        baseline_backend=_passing_baseline,
    )
    assert report.status == "pending_infrastructure"
    assert report.reason == "baseline_profile_mismatch"
    assert report.reference is None


def test_selected_baseline_resources_persist_without_changing_scored_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from revertbench.grade.runner import run_tree
    from revertbench.schema import ExecutionResult
    from revertbench.validate import MachineValidationReport, controller, validate_built_candidate

    calls: list[dict[str, object]] = []
    factories: list[dict[str, object]] = []

    def execute(
        tree: Path,
        submission: str,
        *,
        seed: int,
        timeout_seconds: float = 10,
        output_limit_bytes: int = 65536,
    ) -> ExecutionResult:
        calls.append(dict(timeout_seconds=timeout_seconds, output_limit_bytes=output_limit_bytes))
        return run_tree(
            tree,
            submission,
            seed=seed,
            timeout_seconds=timeout_seconds,
            output_limit_bytes=output_limit_bytes,
        )

    def scored_factory(image: str, *, client: object) -> Any:
        factories.append({"image": image, "client": client})
        return execute

    monkeypatch.setattr(controller, "DockerRunner", scored_factory)
    report = validate_built_candidate(
        _built_toy(tmp_path),
        image="sha256:" + "a" * 64,
        baseline_pids_limit=256,
        baseline_backend=lambda tree: _passing_baseline(tree).model_copy(
            update={"pids_limit": 256}
        ),
        transfer_backend=lambda tree, source: source,
    )
    assert report.status == "machine_validated", report.model_dump_json()
    assert report.baseline_pids_limit == 256
    assert all(row.pids_limit == 256 for row in report.baseline_reports.values())
    assert MachineValidationReport.model_validate_json(report.model_dump_json()) == report
    assert factories == [{"image": "sha256:" + "a" * 64, "client": None}]
    assert calls and all(
        call == {"timeout_seconds": 10, "output_limit_bytes": 65536} for call in calls
    )
    raw = report.model_dump(mode="json")
    raw["baseline_reports"]["bad"]["pids_limit"] = 64
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="resource"):
        MachineValidationReport.model_validate_json(json.dumps(raw))


@pytest.mark.docker
def test_normal_validation_explicit_pid_limit_handles_upstream_threads(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    from dataclasses import replace

    from revertbench.build import canonical_tree_digest
    from revertbench.validate import validate_built_candidate
    from test_baseline_resources import _thread_suite

    built = _built_toy(tmp_path)
    trees = {
        "base": built.base_tree,
        "bad": built.bad_tree,
        "repaired": built.repaired_tree,
        "historical_fix": built.historical_fix_tree,
    }
    for tree in trees.values():
        _thread_suite(tree / "tests")
    built = replace(
        built, source_hashes={role: canonical_tree_digest(tree) for role, tree in trees.items()}
    )
    report = validate_built_candidate(
        built, image=worker_image, client=docker_client, baseline_pids_limit=256
    )
    assert report.status == "machine_validated", report.model_dump_json()
    assert report.baseline_pids_limit == 256
    assert all(
        row.pids_limit == 256 and row.passed == 2 for row in report.baseline_reports.values()
    )
    assert report.reference is not None and report.reference.score == 1


def test_baseline_resource_mismatch_stays_pending_before_reference(tmp_path: Path) -> None:
    from revertbench.validate import validate_built_candidate

    report = validate_built_candidate(
        _built_toy(tmp_path),
        image="sha256:" + "a" * 64,
        baseline_pids_limit=256,
        baseline_backend=_passing_baseline,
    )
    assert report.status == "pending_infrastructure"
    assert report.reason == "baseline_resource_mismatch" and report.reference is None


@pytest.mark.parametrize("limit", [7, 257, True])
def test_invalid_baseline_resource_request_fails_before_execution(
    tmp_path: Path, limit: Any
) -> None:
    from revertbench.validate import validate_built_candidate

    with pytest.raises(ValueError):
        validate_built_candidate(
            _built_toy(tmp_path), image="sha256:" + "a" * 64, baseline_pids_limit=limit
        )
