from pathlib import Path

import pytest
from pydantic import ValidationError

from revertbench.schema import Case, ExecutionResult, GradeResult, RepairSpec


def test_grade_status_requires_matching_score() -> None:
    for status, score in (("passed", 0), ("rejected", 1), ("invalid_run", 0)):
        with pytest.raises(ValidationError):
            GradeResult.model_validate({"status": status, "score": score, "reason": "invalid_run"})


@pytest.mark.parametrize("score", [True, False, "1", 1.0])
def test_grade_score_is_strict(score: object) -> None:
    with pytest.raises(ValidationError):
        GradeResult.model_validate({"status": "passed", "score": score, "reason": "invalid_run"})


def test_grade_result_round_trips_json_and_is_frozen() -> None:
    result = GradeResult(status="invalid_run", score=None, reason="invalid_run")
    assert GradeResult.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        result.reason = "passed"


def test_execution_rejects_unknown_report_fields() -> None:
    with pytest.raises(ValidationError):
        ExecutionResult.model_validate({"status": "completed", "seed": 11, "forced_pass": True})


def test_case_retains_controlled_repair_provenance() -> None:
    repair = RepairSpec(
        patch_path="patches/case.patch", patch_sha256="a" * 64, repaired_tree_sha256="b" * 64
    )
    case = Case(
        id="toy-regression",
        repo="owner/toy",
        base_commit="1" * 40,
        bad_commit="2" * 40,
        fix_commit="3" * 40,
        repair=repair,
        changed_lines={"toy.py": (2, 3)},
    )
    assert case.repair == repair
    assert case.fix_commit != case.bad_commit
    assert Case.model_validate_json(case.model_dump_json()) == case


@pytest.mark.parametrize("path", ["../patch", "/tmp/patch", "a/../patch", "a\\patch"])
def test_repair_path_must_be_relative_and_normalized(path: str) -> None:
    with pytest.raises(ValidationError):
        RepairSpec(patch_path=path, patch_sha256="a" * 64, repaired_tree_sha256="b" * 64)


def test_case_rejects_nonpositive_changed_line() -> None:
    with pytest.raises(ValidationError):
        Case(
            id="toy",
            repo="owner/toy",
            base_commit="1" * 40,
            bad_commit="2" * 40,
            fix_commit="3" * 40,
            repair=RepairSpec(
                patch_path="p.patch", patch_sha256="a" * 64, repaired_tree_sha256="b" * 64
            ),
            changed_lines={str(Path("toy.py")): (0,)},
        )
