"""Reference validation over base, bad, and minimally repaired trusted trees."""

from collections.abc import Collection, Mapping
from pathlib import Path

from revertbench.grade.policy import check_submission
from revertbench.grade.rules import (
    execution_failure,
    grade_submission,
    outcome_vector,
    report_problem,
    result_for,
    validate_inputs,
)
from revertbench.grade.runner import run_tree
from revertbench.schema import GradeReason, GradeResult


def validate_reference(
    base_tree: Path,
    bad_tree: Path,
    repaired_tree: Path,
    submission: str,
    changed_lines: Mapping[str, Collection[int]],
    *,
    seeds: tuple[int, int, int] = (11, 23, 37),
    timeout_seconds: float = 10,
    output_limit_bytes: int = 65536,
) -> GradeResult:
    """Require the same reference to pass before introduction and after repair."""
    try:
        validate_inputs(
            (bad_tree, base_tree, repaired_tree),
            changed_lines,
            seeds,
            timeout_seconds,
            output_limit_bytes,
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
    base_runs = tuple(
        run_tree(
            base_tree,
            submission,
            seed=seed,
            timeout_seconds=timeout_seconds,
            output_limit_bytes=output_limit_bytes,
        )
        for seed in seeds
    )
    if failure := execution_failure(base_runs):
        return result_for(failure, base_runs)
    for seed, run in zip(seeds, base_runs, strict=True):
        if run.seed != seed or (problem := report_problem(run)):
            detail = "worker returned an unexpected seed" if run.seed != seed else problem
            assert detail is not None
            return result_for("invalid_report", base_runs, details=(detail,))
    if len({outcome_vector(run) for run in base_runs}) != 1:
        return result_for("unstable", base_runs)
    if any(test.call and test.call.outcome != "passed" for run in base_runs for test in run.tests):
        return result_for("reference_failed_on_base", base_runs)
    pair = grade_submission(
        bad_tree,
        repaired_tree,
        submission,
        changed_lines,
        seeds=seeds,
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit_bytes,
    )
    executions = base_runs + pair.executions
    conditions = {**pair.conditions, "base_passes": True}
    if pair.status != "passed":
        return pair.model_copy(update={"executions": executions, "conditions": conditions})
    expected_nodes = set(base_runs[0].collected_node_ids)
    if any(set(run.collected_node_ids) != expected_nodes for run in pair.executions):
        return result_for("collection_mismatch", executions, conditions=conditions)
    return pair.model_copy(update={"executions": executions, "conditions": conditions})
