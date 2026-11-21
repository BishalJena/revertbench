"""Differential rules over bounded, structured execution evidence.

The local runner is for trusted fixtures only. Policy admission and coverage are
bounded checks, not a proof of arbitrary Python safety or assertion relevance.
"""

import math
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Literal

from revertbench.grade.coverage_check import (
    changed_line_hits,
    normalize_changed_lines,
    relative_source_path,
)
from revertbench.grade.policy import check_submission
from revertbench.grade.runner import run_tree
from revertbench.schema import ExecutionResult, GradeReason, GradeResult, TestResult

_SUPPORTED_EXCEPTIONS = frozenset(
    {
        "ArithmeticError",
        "IndexError",
        "KeyError",
        "LookupError",
        "NotImplementedError",
        "OverflowError",
        "RuntimeError",
        "TypeError",
        "ValueError",
        "ZeroDivisionError",
    }
)
_CONDITIONS = (
    "submission_policy",
    "fails_on_bad",
    "passes_on_repair",
    "changed_coverage",
    "stable",
)


def result_for(
    reason: GradeReason,
    executions: tuple[ExecutionResult, ...] = (),
    *,
    details: tuple[str, ...] = (),
    conditions: dict[str, bool] | None = None,
) -> GradeResult:
    """Use one explicit score/status mapping for all grading paths."""
    status: Literal["passed", "rejected", "invalid_run"] = "rejected"
    score: int | None = 0
    if reason == "passed":
        status, score = "passed", 1
    elif reason in ("invalid_run", "invalid_input"):
        status, score = "invalid_run", None
    return GradeResult(
        status=status,
        score=score,
        reason=reason,
        conditions=conditions or dict.fromkeys(_CONDITIONS, False),
        executions=executions,
        details=details,
    )


def validate_inputs(
    trees: tuple[Path, ...],
    changed_lines: Mapping[str, Collection[int]],
    seeds: tuple[int, int, int],
    timeout_seconds: float,
    output_limit_bytes: int,
) -> dict[str, frozenset[int]]:
    """Validate trusted case metadata before policy analysis or execution."""
    if any(not tree.is_dir() or tree.is_symlink() for tree in trees):
        raise ValueError("case trees must be existing, non-symlink directories")
    if len(seeds) != 3 or len(set(seeds)) != 3:
        raise ValueError("exactly three distinct seeds are required")
    if any(type(seed) is not int or not 0 <= seed <= 4_294_967_295 for seed in seeds):
        raise ValueError("seeds must be distinct non-negative 32-bit integers")
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or type(output_limit_bytes) is not int
        or output_limit_bytes <= 0
    ):
        raise ValueError("execution time and output limits must be positive and finite")
    return normalize_changed_lines(trees[0], changed_lines)


def execution_failure(executions: tuple[ExecutionResult, ...]) -> GradeReason | None:
    """Infrastructure invalidity dominates any observed submission failure."""
    if any(run.status == "invalid_run" for run in executions):
        return "invalid_run"
    for run in executions:
        if run.status in ("submission_timeout", "submission_output_limit", "submission_error"):
            return run.status
    return None


def _coverage_problem(test: TestResult) -> str | None:
    for path, lines in test.covered_lines.items():
        try:
            relative_source_path(path)
        except ValueError:
            return "coverage contains an invalid project source path"
        if any(type(line) is not int or line < 1 for line in lines):
            return "coverage contains an invalid line number"
    return None


def _test_problem(test: TestResult) -> str | None:
    phases = (test.setup, test.call, test.teardown)
    if any(phase is None for phase in phases):
        return "test phase evidence is incomplete"
    for phase in phases:
        assert phase is not None
        if phase.was_xfail or phase.outcome == "skipped":
            return "skipped or expected-failure outcomes are ineligible"
        if phase.outcome == "passed" and (phase.exception_type or phase.traceback_paths):
            return "passed phase contains contradictory exception evidence"
        if phase.outcome == "failed" and not phase.exception_type:
            return "failed phase lacks exception evidence"
    if test.setup is not None and test.setup.outcome != "passed":
        return "test setup failed"
    if test.teardown is not None and test.teardown.outcome != "passed":
        return "test teardown failed"
    return _coverage_problem(test)


def report_problem(run: ExecutionResult) -> str | None:
    """Reject incomplete or internally contradictory reports before scoring."""
    nodes = run.collected_node_ids
    order = run.execution_order
    if run.status != "completed" or run.collection_errors or run.truncated:
        return "execution did not produce a complete clean collection report"
    if not nodes or any(not node for node in nodes) or len(set(nodes)) != len(nodes):
        return "test collection must be non-empty and unique"
    if len(order) != len(nodes) or set(order) != set(nodes):
        return "execution order must contain every collected test exactly once"
    test_nodes = tuple(test.node_id for test in run.tests)
    if len(test_nodes) != len(nodes) or set(test_nodes) != set(nodes):
        return "test reports must match the collected node identities"
    for test in run.tests:
        if problem := _test_problem(test):
            return problem
    failed = any(test.call is not None and test.call.outcome == "failed" for test in run.tests)
    if run.exit_code != (1 if failed else 0):
        return "worker exit status contradicts test phase outcomes"
    return None


def outcome_vector(run: ExecutionResult) -> tuple[tuple[str, str, str | None], ...]:
    """Canonical call outcomes allow comparing differently shuffled runs."""
    return tuple(
        sorted(
            (test.node_id, test.call.outcome, test.call.exception_type)
            for test in run.tests
            if test.call is not None
        )
    )


def _eligible_failure(test: TestResult, changed: Mapping[str, Collection[int]]) -> bool:
    assert test.call is not None
    exception = (test.call.exception_type or "").removeprefix("builtins.")
    if exception == "AssertionError":
        return True
    return exception in _SUPPORTED_EXCEPTIONS and bool(
        set(test.call.traceback_paths).intersection(changed)
    )


def _pair_grade(
    bad_runs: tuple[ExecutionResult, ...],
    repaired_runs: tuple[ExecutionResult, ...],
    changed: Mapping[str, Collection[int]],
    seeds: tuple[int, int, int],
) -> GradeResult:
    executions = bad_runs + repaired_runs
    conditions = dict.fromkeys(_CONDITIONS, False)
    conditions["submission_policy"] = True
    if failure := execution_failure(executions):
        return result_for(failure, executions, conditions=conditions)
    for runs in (bad_runs, repaired_runs):
        for seed, run in zip(seeds, runs, strict=True):
            if run.seed != seed or (problem := report_problem(run)):
                detail = "worker returned an unexpected seed" if run.seed != seed else problem
                assert detail is not None
                return result_for(
                    "invalid_report", executions, details=(detail,), conditions=conditions
                )
    expected_nodes = set(bad_runs[0].collected_node_ids)
    if any(set(run.collected_node_ids) != expected_nodes for run in executions):
        return result_for("collection_mismatch", executions, conditions=conditions)
    if any(len({outcome_vector(run) for run in runs}) != 1 for runs in (bad_runs, repaired_runs)):
        return result_for("unstable", executions, conditions=conditions)
    conditions["stable"] = True
    failed = tuple(
        test for test in bad_runs[0].tests if test.call and test.call.outcome == "failed"
    )
    if not failed:
        return result_for("bad_did_not_fail", executions, conditions=conditions)
    if any(
        not _eligible_failure(test, changed)
        for run in bad_runs
        for test in run.tests
        if test.call and test.call.outcome == "failed"
    ):
        return result_for("ineligible_failure", executions, conditions=conditions)
    conditions["fails_on_bad"] = True
    if any(
        test.call and test.call.outcome != "passed" for run in repaired_runs for test in run.tests
    ):
        return result_for("repair_did_not_pass", executions, conditions=conditions)
    conditions["passes_on_repair"] = True
    if any(
        not changed_line_hits(test, changed)
        for run in bad_runs
        for test in run.tests
        if test.call and test.call.outcome == "failed"
    ):
        return result_for("coverage_missing", executions, conditions=conditions)
    conditions["changed_coverage"] = True
    return result_for("passed", executions, conditions=conditions)


def grade_submission(
    bad_tree: Path,
    repaired_tree: Path,
    submission: str,
    changed_lines: Mapping[str, Collection[int]],
    *,
    seeds: tuple[int, int, int] = (11, 23, 37),
    timeout_seconds: float = 10,
    output_limit_bytes: int = 65536,
) -> GradeResult:
    """Grade trusted fixtures against bad plus a controlled repaired tree."""
    try:
        changed = validate_inputs(
            (bad_tree, repaired_tree), changed_lines, seeds, timeout_seconds, output_limit_bytes
        )
    except (OSError, UnicodeError, SyntaxError, ValueError) as error:
        return result_for("invalid_input", details=(str(error),))
    try:
        policy = check_submission(submission, bad_tree)
    except OSError:
        return result_for("invalid_run", details=("case source cannot be read",))
    if not policy.allowed:
        reason: GradeReason = (
            "invalid_run" if policy.reason == "project_unavailable" else "policy_rejected"
        )
        return result_for(reason, details=(policy.reason or "policy rejected", *policy.details))
    runs: list[tuple[ExecutionResult, ...]] = []
    for tree in (bad_tree, repaired_tree):
        runs.append(
            tuple(
                run_tree(
                    tree,
                    submission,
                    seed=seed,
                    timeout_seconds=timeout_seconds,
                    output_limit_bytes=output_limit_bytes,
                )
                for seed in seeds
            )
        )
    return _pair_grade(runs[0], runs[1], changed, seeds)
