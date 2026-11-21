"""Versioned provenance and structured evidence for differential grading."""

from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CommitId = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
ContentDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or "\\" in value or path.is_absolute() or ".." in path.parts:
        raise ValueError("path must be a normalized relative POSIX path")
    if str(path) != value or value == ".":
        raise ValueError("path must be a normalized relative POSIX path")
    return value


class EvidenceModel(BaseModel):
    """Reject unknown fields and implicit coercion at the worker boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RepairSpec(EvidenceModel):
    patch_path: str
    patch_sha256: ContentDigest
    repaired_tree_sha256: ContentDigest

    _validate_patch_path = field_validator("patch_path")(_relative_path)


class Case(EvidenceModel):
    schema_version: Literal["2"] = "2"
    id: Annotated[str, Field(min_length=1)]
    repo: Annotated[str, Field(min_length=1)]
    base_commit: CommitId
    bad_commit: CommitId
    fix_commit: CommitId
    repair: RepairSpec
    changed_lines: dict[str, tuple[int, ...]] = Field(default_factory=dict)

    @field_validator("changed_lines")
    @classmethod
    def validate_changed_lines(
        cls, value: dict[str, tuple[int, ...]]
    ) -> dict[str, tuple[int, ...]]:
        for path, lines in value.items():
            _relative_path(path)
            if any(line < 1 for line in lines):
                raise ValueError("changed line numbers must be positive")
        return value


class PhaseResult(EvidenceModel):
    outcome: Literal["passed", "failed", "skipped"]
    exception_type: str | None = None
    traceback_paths: tuple[str, ...] = ()
    was_xfail: bool = False


class TestResult(EvidenceModel):
    node_id: str
    setup: PhaseResult | None = None
    call: PhaseResult | None = None
    teardown: PhaseResult | None = None
    covered_lines: dict[str, tuple[int, ...]] = Field(default_factory=dict)


class ExecutionResult(EvidenceModel):
    status: Literal[
        "completed",
        "submission_timeout",
        "submission_output_limit",
        "submission_error",
        "invalid_run",
    ]
    seed: int
    exit_code: int | None = None
    collected_node_ids: tuple[str, ...] = ()
    execution_order: tuple[str, ...] = ()
    tests: tuple[TestResult, ...] = ()
    collection_errors: tuple[str, ...] = ()
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    reason: str | None = None


GradeReason = Literal[
    "passed",
    "policy_rejected",
    "invalid_input",
    "invalid_run",
    "bad_did_not_fail",
    "repair_did_not_pass",
    "ineligible_failure",
    "coverage_missing",
    "unstable",
    "collection_mismatch",
    "invalid_report",
    "submission_timeout",
    "submission_output_limit",
    "submission_error",
    "reference_failed_on_base",
]


class GradeResult(EvidenceModel):
    status: Literal["passed", "rejected", "invalid_run"]
    score: Annotated[int | None, Field(ge=0, le=1)]
    reason: GradeReason
    conditions: dict[str, bool] = Field(default_factory=dict)
    executions: tuple[ExecutionResult, ...] = ()
    details: tuple[str, ...] = ()

    @model_validator(mode="after")
    def check_score(self) -> Self:
        expected = {"passed": 1, "rejected": 0, "invalid_run": None}[self.status]
        if self.score != expected:
            raise ValueError("score must be 1 for passed, 0 for rejected and null for invalid_run")
        return self
