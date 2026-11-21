"""Adversarial evidence checks for the differential grading contract."""

from collections.abc import Collection, Mapping
from pathlib import Path

import pytest
from docker import DockerClient

from revertbench.grade import rules
from revertbench.grade.coverage_check import changed_line_hits, normalize_changed_lines
from revertbench.grade.policy import PolicyResult
from revertbench.schema import ExecutionResult, PhaseResult
from revertbench.schema import TestResult as NodeResult

SUBMISSION = (
    "from project import normalize\n\ndef test_regression():\n    assert normalize(' X ') == 'x'\n"
)
NODE = "tests/test_submission.py::test_regression"
OTHER = "tests/test_submission.py::test_other"
SEEDS = (11, 23, 37)


def _trees(tmp_path: Path) -> tuple[Path, Path]:
    bad, repaired = tmp_path / "bad", tmp_path / "repaired"
    for tree in (bad, repaired):
        tree.mkdir()
    (bad / "project.py").write_text("def normalize(value):\n    return value\n")
    (repaired / "project.py").write_text(
        "def normalize(value):\n    return value.strip().lower()\n"
    )
    return bad, repaired


def _test(
    *,
    failed: bool = False,
    node: str = NODE,
    covered: dict[str, tuple[int, ...]] | None = None,
    exception_type: str = "AssertionError",
    traceback_paths: tuple[str, ...] = (),
) -> NodeResult:
    return NodeResult(
        node_id=node,
        setup=PhaseResult(outcome="passed"),
        call=PhaseResult(
            outcome="failed" if failed else "passed",
            exception_type=exception_type if failed else None,
            traceback_paths=traceback_paths,
        ),
        teardown=PhaseResult(outcome="passed"),
        covered_lines=covered if covered is not None else {"project.py": (2,)},
    )


def _execution(*tests: NodeResult, seed: int = 11) -> ExecutionResult:
    failed = any(test.call is not None and test.call.outcome == "failed" for test in tests)
    nodes = tuple(test.node_id for test in tests)
    return ExecutionResult(
        status="completed",
        seed=seed,
        exit_code=1 if failed else 0,
        collected_node_ids=nodes,
        execution_order=nodes,
        tests=tests,
    )


def _install_runs(
    monkeypatch: pytest.MonkeyPatch,
    bad: ExecutionResult,
    repaired: ExecutionResult,
    *,
    changes: dict[tuple[str, int], ExecutionResult] | None = None,
) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []

    def run(
        tree: Path,
        submission: str,
        *,
        seed: int,
        timeout_seconds: float = 10,
        output_limit_bytes: int = 65536,
    ) -> ExecutionResult:
        calls.append((tree.name, seed))
        if changes is not None and (tree.name, seed) in changes:
            return changes[(tree.name, seed)]
        result = bad if tree.name == "bad" else repaired
        return result.model_copy(update={"seed": seed})

    monkeypatch.setattr(rules, "run_tree", run)
    return calls


def test_meaningful_differential_pass_runs_three_seeded_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad, repaired = _trees(tmp_path)
    calls = _install_runs(monkeypatch, _execution(_test(failed=True)), _execution(_test()))

    result = rules.grade_submission(bad, repaired, SUBMISSION, {"project.py": {2}})

    assert result.status == "passed"
    assert result.score == 1
    assert result.reason == "passed"
    assert set(calls) == {(tree.name, seed) for tree in (bad, repaired) for seed in SEEDS}
    assert len(calls) == 6
    assert len(result.executions) == 6


@pytest.mark.parametrize("bad_fails,repair_fails", [(False, False), (True, True)])
def test_always_passing_or_always_failing_submission_scores_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_fails: bool, repair_fails: bool
) -> None:
    bad, repaired = _trees(tmp_path)
    _install_runs(
        monkeypatch, _execution(_test(failed=bad_fails)), _execution(_test(failed=repair_fails))
    )

    result = rules.grade_submission(bad, repaired, SUBMISSION, {"project.py": {2}})

    assert result.status == "rejected"
    assert result.score == 0


@pytest.mark.parametrize(
    "exception_type",
    [
        "ImportError",
        "ModuleNotFoundError",
        "NameError",
        "AttributeError",
        "SyntaxError",
        "SystemExit",
        "project.AssertionError",
        "project.ValueError",
    ],
)
def test_excluded_exception_never_counts_even_with_changed_code_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exception_type: str
) -> None:
    bad, repaired = _trees(tmp_path)
    _install_runs(
        monkeypatch,
        _execution(
            _test(failed=True, exception_type=exception_type, traceback_paths=("project.py",))
        ),
        _execution(_test()),
    )

    result = rules.grade_submission(bad, repaired, SUBMISSION, {"project.py": {2}})

    assert result.reason == "ineligible_failure"
    assert result.score == 0


def test_supported_project_exception_must_originate_in_changed_project_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad, repaired = _trees(tmp_path)
    _install_runs(
        monkeypatch,
        _execution(
            _test(failed=True, exception_type="ValueError", traceback_paths=("tests/test.py",))
        ),
        _execution(_test()),
    )

    assert rules.grade_submission(bad, repaired, SUBMISSION, {"project.py": {2}}).score == 0

    _install_runs(
        monkeypatch,
        _execution(
            _test(
                failed=True, exception_type="builtins.ValueError", traceback_paths=("project.py",)
            )
        ),
        _execution(_test()),
    )
    assert rules.grade_submission(bad, repaired, SUBMISSION, {"project.py": {2}}).score == 1


def test_failing_node_cannot_borrow_changed_coverage_from_other_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad, repaired = _trees(tmp_path)
    _install_runs(
        monkeypatch,
        _execution(_test(failed=True, covered={}), _test(node=OTHER)),
        _execution(_test(), _test(node=OTHER)),
    )

    result = rules.grade_submission(bad, repaired, SUBMISSION, {"project.py": {2}})

    assert result.reason == "coverage_missing"
    assert result.score == 0


def test_every_failed_node_must_have_eligible_failure_and_own_changed_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad, repaired = _trees(tmp_path)
    _install_runs(
        monkeypatch,
        _execution(_test(failed=True), _test(failed=True, node=OTHER, covered={})),
        _execution(_test(), _test(node=OTHER)),
    )
    assert rules.grade_submission(bad, repaired, SUBMISSION, {"project.py": {2}}).score == 0


@pytest.mark.parametrize("phase", ["setup", "teardown"])
def test_non_call_failures_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    bad, repaired = _trees(tmp_path)
    test = _test(failed=True).model_copy(
        update={phase: PhaseResult(outcome="failed", exception_type="AssertionError")}
    )
    _install_runs(monkeypatch, _execution(test), _execution(_test()))
    assert rules.grade_submission(bad, repaired, SUBMISSION, {"project.py": {2}}).score == 0


@pytest.mark.parametrize(
    "call",
    [
        PhaseResult(outcome="skipped"),
        PhaseResult(outcome="failed", exception_type="AssertionError", was_xfail=True),
        PhaseResult(outcome="passed", was_xfail=True),
    ],
)
def test_skip_xfail_and_xpass_never_count_as_valid_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: PhaseResult
) -> None:
    bad, repaired = _trees(tmp_path)
    test = _test().model_copy(update={"call": call})
    _install_runs(monkeypatch, _execution(test), _execution(_test()))
    assert rules.grade_submission(bad, repaired, SUBMISSION, {"project.py": {2}}).score == 0


@pytest.mark.parametrize(
    "updates",
    [
        {"collected_node_ids": ()},
        {"collected_node_ids": (NODE, NODE)},
        {"tests": ()},
        {"execution_order": ()},
        {"execution_order": (NODE, NODE)},
        {"exit_code": 0},
        {"exit_code": None},
        {"collection_errors": ("collection failed",)},
        {"truncated": True},
    ],
)
def test_incomplete_or_contradictory_reports_cannot_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, updates: dict[str, object]
) -> None:
    bad, repaired = _trees(tmp_path)
    execution = _execution(_test(failed=True)).model_copy(update=updates)
    _install_runs(monkeypatch, execution, _execution(_test()))
    assert rules.grade_submission(bad, repaired, SUBMISSION, {"project.py": {2}}).score == 0


def test_missing_phase_and_exception_on_passed_phase_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad, repaired = _trees(tmp_path)
    for malformed in (
        _test(failed=True).model_copy(update={"teardown": None}),
        _test(failed=True).model_copy(
            update={"setup": PhaseResult(outcome="passed", exception_type="ValueError")}
        ),
    ):
        _install_runs(monkeypatch, _execution(malformed), _execution(_test()))
        assert rules.grade_submission(bad, repaired, SUBMISSION, {"project.py": {2}}).score == 0


def test_test_collection_must_match_between_scored_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad, repaired = _trees(tmp_path)
    _install_runs(monkeypatch, _execution(_test(failed=True)), _execution(_test(node=OTHER)))
    result = rules.grade_submission(bad, repaired, SUBMISSION, {"project.py": {2}})
    assert result.reason == "collection_mismatch"
    assert result.score == 0


def test_same_bad_failure_nodes_must_repeat_across_seeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad, repaired = _trees(tmp_path)
    initial = _execution(_test(failed=True), _test(node=OTHER))
    changed = _execution(_test(), _test(failed=True, node=OTHER), seed=23)
    _install_runs(
        monkeypatch,
        initial,
        _execution(_test(), _test(node=OTHER)),
        changes={("bad", 23): changed},
    )
    result = rules.grade_submission(bad, repaired, SUBMISSION, {"project.py": {2}})
    assert result.reason == "unstable"
    assert result.score == 0


def test_infrastructure_failure_overrides_behavioral_rejection_and_has_no_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad, repaired = _trees(tmp_path)
    _install_runs(
        monkeypatch,
        _execution(_test()),
        ExecutionResult(status="invalid_run", seed=11, reason="worker_start_failed"),
    )
    result = rules.grade_submission(bad, repaired, SUBMISSION, {"project.py": {2}})
    assert result.status == "invalid_run"
    assert result.score is None
    assert result.reason == "invalid_run"


@pytest.mark.parametrize(
    "status", ["submission_timeout", "submission_output_limit", "submission_error"]
)
def test_established_submission_resource_or_execution_failures_score_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    bad, repaired = _trees(tmp_path)
    failure = ExecutionResult.model_validate({"status": status, "seed": 11})
    _install_runs(monkeypatch, failure, _execution(_test()))
    result = rules.grade_submission(bad, repaired, SUBMISSION, {"project.py": {2}})
    assert result.status == "rejected"
    assert result.score == 0
    assert result.reason == status


@pytest.mark.parametrize(
    "changed",
    [
        {},
        {"../project.py": {2}},
        {"/project.py": {2}},
        {"project.py": {0}},
        {"missing.py": {2}},
        {"project.py": {99}},
    ],
)
def test_invalid_changed_hunks_rejected_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: Mapping[str, Collection[int]]
) -> None:
    bad, repaired = _trees(tmp_path)
    calls = _install_runs(monkeypatch, _execution(_test(failed=True)), _execution(_test()))
    result = rules.grade_submission(bad, repaired, SUBMISSION, changed)
    assert result.reason == "invalid_input"
    assert result.status == "invalid_run"
    assert result.score is None
    assert calls == []


@pytest.mark.parametrize("seeds", [(1, 1, 1), (1, 2), (-1, 2, 3), (True, 2, 3)])
def test_invalid_seed_contract_rejected_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seeds: tuple[int, ...]
) -> None:
    bad, repaired = _trees(tmp_path)
    calls = _install_runs(monkeypatch, _execution(_test(failed=True)), _execution(_test()))
    result = rules.grade_submission(
        bad,
        repaired,
        SUBMISSION,
        {"project.py": {2}},
        seeds=seeds,  # type: ignore[arg-type]
    )
    assert result.reason == "invalid_input"
    assert result.status == "invalid_run"
    assert result.score is None
    assert calls == []


def test_changed_hunks_need_executable_lines_and_hits_are_per_node(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "project.py").write_text("# comment\ndef normalize(value):\n    return value\n")
    with pytest.raises(ValueError, match="executable"):
        normalize_changed_lines(tree, {"project.py": {1}})
    changed = normalize_changed_lines(tree, {"project.py": {1, 3}})
    assert changed == {"project.py": frozenset({3})}
    assert changed_line_hits(_test(covered={"project.py": (3,)}), changed) == {"project.py": (3,)}
    assert changed_line_hits(_test(covered={}), changed) == {}


def test_policy_rejects_introspection_without_running_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad, repaired = _trees(tmp_path)
    calls = _install_runs(monkeypatch, _execution(_test(failed=True)), _execution(_test()))
    submission = (
        "import project\n\ndef test_cheat():\n"
        "    project.normalize('x')\n"
        "    assert hasattr(project, 'new_symbol')\n"
    )
    result = rules.grade_submission(bad, repaired, submission, {"project.py": {2}})
    assert result.reason == "policy_rejected"
    assert result.score == 0
    assert calls == []


@pytest.mark.parametrize("limit", [0, -1, float("inf"), float("nan"), True])
def test_invalid_execution_limits_are_case_errors_without_model_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: float
) -> None:
    bad, repaired = _trees(tmp_path)
    calls = _install_runs(monkeypatch, _execution(_test(failed=True)), _execution(_test()))
    result = rules.grade_submission(
        bad, repaired, SUBMISSION, {"project.py": {2}}, timeout_seconds=limit
    )
    assert result.status == "invalid_run"
    assert result.reason == "invalid_input"
    assert result.score is None
    assert calls == []


def test_missing_case_tree_is_not_a_model_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad, repaired = _trees(tmp_path)
    calls = _install_runs(monkeypatch, _execution(_test(failed=True)), _execution(_test()))
    result = rules.grade_submission(bad, repaired / "missing", SUBMISSION, {"project.py": {2}})
    assert result.status == "invalid_run"
    assert result.score is None
    assert calls == []


def test_changed_source_symlink_is_rejected_before_read_or_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad, repaired = _trees(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("raise RuntimeError('never execute source during hunk analysis')\n")
    (bad / "project.py").unlink()
    (bad / "project.py").symlink_to(outside)
    calls = _install_runs(monkeypatch, _execution(_test(failed=True)), _execution(_test()))
    result = rules.grade_submission(bad, repaired, SUBMISSION, {"project.py": {1}})
    assert result.status == "invalid_run"
    assert result.score is None
    assert calls == []


def test_real_changed_project_exception_can_expose_the_regression(tmp_path: Path) -> None:
    bad, repaired = _trees(tmp_path)
    (bad / "project.py").write_text("def normalize(value):\n    raise ValueError('regression')\n")
    result = rules.grade_submission(bad, repaired, SUBMISSION, {"project.py": {2}})
    assert result.status == "passed", result.model_dump()
    assert result.score == 1


@pytest.mark.parametrize("raises", [False, True])
def test_policy_project_access_failure_is_not_a_model_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raises: bool
) -> None:
    bad, repaired = _trees(tmp_path)
    calls = _install_runs(monkeypatch, _execution(_test(failed=True)), _execution(_test()))

    def unavailable(source: str, tree: Path) -> PolicyResult:
        if raises:
            raise PermissionError("case source inaccessible")
        return PolicyResult(False, "project_unavailable", ("case source inaccessible",))

    monkeypatch.setattr(rules, "check_submission", unavailable)
    result = rules.grade_submission(bad, repaired, SUBMISSION, {"project.py": {2}})
    assert result.status == "invalid_run"
    assert result.score is None
    assert calls == []


def test_selected_execution_backend_is_used_for_every_scored_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad, repaired = _trees(tmp_path)
    calls: list[tuple[Path, int, float, int]] = []

    def host_forbidden(
        tree: Path,
        submission: str,
        *,
        seed: int,
        timeout_seconds: float = 10,
        output_limit_bytes: int = 65536,
    ) -> ExecutionResult:
        raise AssertionError("selected backend must never fall back to host execution")

    def selected(
        tree: Path,
        submission: str,
        *,
        seed: int,
        timeout_seconds: float = 10,
        output_limit_bytes: int = 65536,
    ) -> ExecutionResult:
        assert submission == SUBMISSION
        calls.append((tree, seed, timeout_seconds, output_limit_bytes))
        return _execution(_test(failed=tree == bad), seed=seed)

    monkeypatch.setattr(rules, "run_tree", host_forbidden)
    result = rules.grade_submission(
        bad,
        repaired,
        SUBMISSION,
        {"project.py": {2}},
        timeout_seconds=21,
        output_limit_bytes=4096,
        execution_backend=selected,
    )
    assert result.status == "passed"
    assert set(calls) == {(tree, seed, 21, 4096) for tree in (bad, repaired) for seed in SEEDS}
    assert len(calls) == 6


def test_static_rejection_does_not_invoke_selected_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad, repaired = _trees(tmp_path)

    def forbidden(
        tree: Path,
        submission: str,
        *,
        seed: int,
        timeout_seconds: float = 10,
        output_limit_bytes: int = 65536,
    ) -> ExecutionResult:
        raise AssertionError("static rejection must precede every execution backend")

    monkeypatch.setattr(rules, "run_tree", forbidden)
    source = "import os\ndef test_bad():\n    assert os.environ\n"
    result = rules.grade_submission(
        bad,
        repaired,
        source,
        {"project.py": {2}},
        execution_backend=forbidden,
    )
    assert result.reason == "policy_rejected"
    assert result.score == 0


@pytest.mark.docker
@pytest.mark.parametrize(
    "mode", ["always_pass", "always_fail", "missing_dependency", "flaky", "skip", "exception_alias"]
)
def test_docker_backend_enforces_the_same_differential_conditions(
    tmp_path: Path, docker_client: DockerClient, worker_image: str, mode: str
) -> None:
    from revertbench.sandbox.worker import DockerRunner

    bad, repaired = _trees(tmp_path)
    source = SUBMISSION
    changed = {"project.py": {2}}
    if mode == "always_pass":
        source = (
            "from project import normalize\ndef test_regression():\n"
            "    assert normalize('x') == 'x'\n"
        )
    elif mode == "always_fail":
        source = (
            "from project import normalize\ndef test_regression():\n"
            "    normalize('x')\n    assert False\n"
        )
    elif mode == "missing_dependency":
        (bad / "project.py").write_text(
            "def normalize(value):\n    from nonexistent_case_dependency import unused\n"
            "    return unused\n"
        )
    elif mode == "flaky":
        (bad / "project.py").write_text(
            "def normalize(value):\n    import random\n"
            "    return value if random.random() < 0.5 else value.strip().lower()\n"
        )
        changed = {"project.py": {3}}
    elif mode == "skip":
        (bad / "project.py").write_text(
            "def normalize(value):\n    import pytest\n"
            "    pytest.skip('not a regression failure')\n"
        )
    elif mode == "exception_alias":
        (bad / "project.py").write_text(
            "class ValueError(Exception):\n    pass\n\n"
            "def normalize(value):\n    raise ValueError('not the builtin exception')\n"
        )
        changed = {"project.py": {5}}
    backend = DockerRunner(worker_image, client=docker_client)

    result = rules.grade_submission(bad, repaired, source, changed, execution_backend=backend)

    assert result.status == "rejected", result.model_dump()
    assert result.score == 0
    assert len(result.executions) == 6


@pytest.mark.docker
def test_docker_backend_cannot_borrow_coverage_from_a_different_test(
    tmp_path: Path, docker_client: DockerClient, worker_image: str
) -> None:
    from revertbench.sandbox.worker import DockerRunner

    bad, repaired = _trees(tmp_path)
    with (bad / "project.py").open("a") as stream:
        stream.write("\ndef unrelated():\n    return 'old'\n")
    with (repaired / "project.py").open("a") as stream:
        stream.write("\ndef unrelated():\n    return 'new'\n")
    source = (
        "from project import normalize, unrelated\n"
        "def test_regression():\n    assert normalize(' X ') == 'x'\n"
        "def test_borrow():\n    assert unrelated() == 'new'\n"
    )
    result = rules.grade_submission(
        bad,
        repaired,
        source,
        {"project.py": {2}},
        execution_backend=DockerRunner(worker_image, client=docker_client),
    )
    assert result.status == "rejected", result.model_dump()
    assert result.reason == "coverage_missing"


@pytest.mark.docker
def test_docker_backend_still_rejects_static_cheats_without_execution(
    tmp_path: Path, docker_client: DockerClient, worker_image: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from revertbench.sandbox.worker import DockerRunner

    bad, repaired = _trees(tmp_path)
    backend = DockerRunner(worker_image, client=docker_client)

    def forbidden(
        tree: Path,
        submission: str,
        *,
        seed: int,
        timeout_seconds: float = 10,
        output_limit_bytes: int = 65536,
    ) -> ExecutionResult:
        raise AssertionError("a policy rejection must not execute in any environment")

    monkeypatch.setattr(rules, "run_tree", forbidden)
    source = (
        "import project\ndef test_cheat():\n    project.normalize('x')\n"
        "    assert hasattr(project, 'repair_only')\n"
    )
    result = rules.grade_submission(
        bad, repaired, source, {"project.py": {2}}, execution_backend=backend
    )
    assert result.reason == "policy_rejected"
    assert result.score == 0
    assert result.executions == ()
