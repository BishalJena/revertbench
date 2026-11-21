"""Three-tree validation must isolate the original regression from later history."""

from pathlib import Path

import pytest

from revertbench.grade import validation
from revertbench.grade.policy import PolicyResult
from revertbench.grade.rules import grade_submission
from revertbench.schema import ExecutionResult, PhaseResult
from revertbench.schema import TestResult as NodeResult

NODE = "tests/test_submission.py::test_regression"
SUBMISSION = (
    "from project import normalize\n\ndef test_regression():\n    assert normalize(' X ') == 'x'\n"
)


def _trees(tmp_path: Path) -> tuple[Path, Path, Path]:
    base, bad, repaired = (tmp_path / name for name in ("base", "bad", "repaired"))
    for tree in (base, bad, repaired):
        tree.mkdir()
        body = "value" if tree == bad else "value.strip().lower()"
        (tree / "project.py").write_text(f"def normalize(value):\n    return {body}\n")
    return base, bad, repaired


def _execution(seed: int, *, failed: bool = False, node: str = NODE) -> ExecutionResult:
    return ExecutionResult(
        status="completed",
        seed=seed,
        exit_code=1 if failed else 0,
        collected_node_ids=(node,),
        execution_order=(node,),
        tests=(
            NodeResult(
                node_id=node,
                setup=PhaseResult(outcome="passed"),
                call=PhaseResult(
                    outcome="failed" if failed else "passed",
                    exception_type="AssertionError" if failed else None,
                ),
                teardown=PhaseResult(outcome="passed"),
                covered_lines={"project.py": (2,)},
            ),
        ),
    )


@pytest.mark.parametrize("base_result", ["failed", "different_collection", "invalid_run", "passed"])
def test_base_must_pass_same_reference_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_result: str
) -> None:
    base, bad, repaired = _trees(tmp_path)

    def run(
        tree: Path,
        submission: str,
        *,
        seed: int,
        timeout_seconds: float = 10,
        output_limit_bytes: int = 65536,
    ) -> ExecutionResult:
        if tree == base:
            if base_result == "invalid_run":
                return ExecutionResult(status="invalid_run", seed=seed, reason="image_missing")
            return _execution(
                seed,
                failed=base_result == "failed",
                node="tests/test_submission.py::different"
                if base_result == "different_collection"
                else NODE,
            )
        return _execution(seed, failed=tree == bad)

    monkeypatch.setattr(validation, "run_tree", run)
    monkeypatch.setattr("revertbench.grade.rules.run_tree", run)
    result = validation.validate_reference(base, bad, repaired, SUBMISSION, {"project.py": {2}})

    if base_result == "invalid_run":
        assert result.status == "invalid_run"
        assert result.score is None
    elif base_result == "passed":
        assert result.status == "passed"
        assert result.score == 1
        assert result.conditions["base_passes"]
        assert len(result.executions) == 9
    else:
        assert result.status == "rejected"
        assert result.score == 0
        assert result.reason in {"reference_failed_on_base", "collection_mismatch"}


def test_toy_reference_passes_base_fails_bad_passes_minimal_repair(tmp_path: Path) -> None:
    base, bad, repaired = _trees(tmp_path)
    result = validation.validate_reference(base, bad, repaired, SUBMISSION, {"project.py": {2}})
    assert result.status == "passed", result.model_dump()
    assert result.score == 1


def test_unrelated_historical_fix_change_cannot_earn_differential_credit(tmp_path: Path) -> None:
    base, bad, repaired = _trees(tmp_path)
    historical = tmp_path / "historical_fix"
    historical.mkdir()
    for tree in (base, bad, repaired):
        with (tree / "project.py").open("a") as stream:
            stream.write("\ndef unrelated():\n    return 'old'\n")
    (historical / "project.py").write_text(
        "def normalize(value):\n    return value.strip().lower()\n"
        "\ndef unrelated():\n    return 'new'\n"
    )
    submission = (
        "from project import normalize, unrelated\n\ndef test_regression():\n"
        "    normalize(' X ')\n    assert unrelated() == 'new'\n"
    )

    result = validation.validate_reference(base, bad, repaired, submission, {"project.py": {2}})

    assert result.score == 0
    assert result.status == "rejected"

    # The same coverage-plus-version test passes a confounded historical pair,
    # but fails the controlled pair even without the base-tree validation gate.
    assert grade_submission(bad, historical, submission, {"project.py": {2}}).score == 1
    assert grade_submission(bad, repaired, submission, {"project.py": {2}}).score == 0


def test_reference_policy_access_failure_has_no_model_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, bad, repaired = _trees(tmp_path)

    def unavailable(source: str, tree: Path) -> PolicyResult:
        return PolicyResult(False, "project_unavailable", ("case source inaccessible",))

    monkeypatch.setattr(validation, "check_submission", unavailable)
    result = validation.validate_reference(base, bad, repaired, SUBMISSION, {"project.py": {2}})
    assert result.status == "invalid_run"
    assert result.score is None
