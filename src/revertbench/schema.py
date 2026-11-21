"""Versioned provenance and structured evidence for differential grading."""

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

CommitId = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
ContentDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or ".." in path.parts
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
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


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("text must not be blank")
    return value


def _public_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("evidence URL must be HTTP(S) without embedded credentials")
    return value


class PullRequestMetadata(EvidenceModel):
    number: Annotated[int, Field(gt=0)] | None = None
    title: str = ""
    body: str = ""
    diff: str | None = None
    url: str | None = None
    published_at: AwareDatetime | None = None
    merged_at: AwareDatetime | None = None
    observed_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    body_snapshot: Literal["unknown", "current_observed", "historical_verified"] = "unknown"
    association_verified: bool = False
    associated_commit: CommitId | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        return None if value is None else _public_url(value)


class ReferenceTest(EvidenceModel):
    path: str
    node_ids: tuple[str, ...]
    source_artifact: str | None = None
    source_sha256: ContentDigest | None = None

    _validate_path = field_validator("path")(_relative_path)

    @field_validator("source_artifact")
    @classmethod
    def validate_artifact(cls, value: str | None) -> str | None:
        return None if value is None else _relative_path(value)

    @field_validator("node_ids")
    @classmethod
    def validate_nodes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("reference node IDs must be nonempty and unique")
        for node in value:
            source, separator, name = node.partition("::")
            _relative_path(source)
            if not separator or not name or any(ord(character) < 32 for character in node):
                raise ValueError("reference node IDs must identify tests in relative source paths")
        return value


class PublicationEvidence(EvidenceModel):
    artifact: Annotated[str, Field(min_length=1)]
    url: str
    published_at: AwareDatetime | None = None
    observed_at: AwareDatetime

    _validate_artifact = field_validator("artifact")(_nonblank)
    _validate_url = field_validator("url")(_public_url)


class CaseEnvironment(EvidenceModel):
    image: str
    python: Annotated[str, Field(pattern=r"^3\.12\.\d+$")]
    dependency_lock_sha256: ContentDigest | None = None
    install: tuple[str, ...] = ()
    test_cmd: tuple[str, ...] = ()

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        if re.fullmatch(r"(?:sha256:[0-9a-f]{64}|[^\s@]+@sha256:[0-9a-f]{64})", value) is None:
            raise ValueError("environment image must identify immutable content")
        return value

    @field_validator("install", "test_cmd")
    @classmethod
    def validate_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 256 or any(not argument or "\x00" in argument for argument in value):
            raise ValueError("recorded commands must be bounded argv without NUL bytes")
        return value


ValidationStatus = Literal[
    "candidate",
    "machine_validated",
    "pending_review",
    "pending_human_review",
    "accepted",
    "rejected",
    "pending_infrastructure",
]
DatasetSplit = Literal["unassigned", "train", "validation", "test"]
ReviewDecision = Literal["accept", "reject", "notes"]
ReviewerKind = Literal["human", "agent"]


class CaseValidation(EvidenceModel):
    status: ValidationStatus = "candidate"
    report: str | None = None
    report_sha256: ContentDigest | None = None
    machine_passed: bool = False
    reason: str | None = None
    grader_version: str | None = None
    policy_version: str | None = None
    grader_sha256: ContentDigest | None = None
    policy_sha256: ContentDigest | None = None
    validator_version: str | None = None

    @field_validator("report")
    @classmethod
    def validate_report(cls, value: str | None) -> str | None:
        return None if value is None else _relative_path(value)

    @model_validator(mode="after")
    def check_machine_status(self) -> Self:
        if self.status in {
            "machine_validated",
            "pending_review",
            "pending_human_review",
            "accepted",
        } and (not self.machine_passed or self.report is None or self.report_sha256 is None):
            raise ValueError("validated status requires a passing machine report and its digest")
        return self


class HumanReview(EvidenceModel):
    review_version: Literal["1"] = "1"
    decision: ReviewDecision
    reviewer: Annotated[str, Field(min_length=1)]
    notes: Annotated[str, Field(min_length=1)]
    reviewed_at: AwareDatetime
    attested: bool = False
    validation_report_sha256: ContentDigest | None = None
    case_inputs_sha256: ContentDigest | None = None

    _validate_text = field_validator("reviewer", "notes")(_nonblank)


class AgentReview(EvidenceModel):
    """An authorized agent's own decision, never a human attestation."""

    review_version: Literal["1"] = "1"
    reviewer_kind: Literal["agent"] = "agent"
    decision: ReviewDecision
    reviewer: Annotated[str, Field(min_length=1)]
    authorization_note: Annotated[str, Field(min_length=1)]
    notes: Annotated[str, Field(min_length=1)]
    reviewed_at: AwareDatetime
    validation_report_sha256: ContentDigest
    case_inputs_sha256: ContentDigest

    _validate_text = field_validator("reviewer", "authorization_note", "notes")(_nonblank)


class CaseArtifact(EvidenceModel):
    path: str
    sha256: ContentDigest

    _validate_path = field_validator("path")(_relative_path)


class RepairDerivation(EvidenceModel):
    """Recomputed provenance for an explicit historical function-body repair."""

    version: Literal["historical_function_body_v1"] = "historical_function_body_v1"
    path: str
    function: str
    signature_sha256: ContentDigest
    before_body_sha256: ContentDigest
    after_body_sha256: ContentDigest
    derived_patch_sha256: ContentDigest
    historical_diff: CaseArtifact

    _validate_path = field_validator("path")(_relative_path)

    @field_validator("function")
    @classmethod
    def validate_function(cls, value: str) -> str:
        if not value.isidentifier():
            raise ValueError("repair target must be one top-level function name")
        return value

    @model_validator(mode="after")
    def check_historical_artifact(self) -> Self:
        if self.historical_diff.path != "historical-production.diff":
            raise ValueError("derived repairs require the fixed historical production artifact")
        return self

    def content_digest(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


class GlobalSuiteEvidence(EvidenceModel):
    """An independently observed CI result applied to a compatible case image.

    applies_to_image does not claim that CI executed that platform-specific image ID.
    Matching grader/policy fingerprints identify the tested implementation.
    """

    ci_url: str
    implementation_commit: CommitId
    applies_to_image: str
    grader_sha256: ContentDigest
    policy_sha256: ContentDigest
    observed_at: AwareDatetime
    checks_status: Literal["passed"]
    sandbox_status: Literal["passed"]

    _validate_url = field_validator("ci_url")(_public_url)

    @field_validator("applies_to_image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        return CaseEnvironment.validate_image(value)


class Case(EvidenceModel):
    schema_version: Literal["2"] = "2"
    id: Annotated[str, Field(min_length=1)]
    repo: Annotated[str, Field(min_length=1)]
    base_commit: CommitId
    bad_commit: CommitId
    fix_commit: CommitId
    repair: RepairSpec
    repair_derivation: RepairDerivation | None = None
    changed_lines: dict[str, tuple[int, ...]] = Field(default_factory=dict)
    source_hashes: dict[str, ContentDigest] = Field(default_factory=dict)
    global_suite_evidence: GlobalSuiteEvidence | None = None
    regression_group_id: str | None = None
    dataset_split: DatasetSplit = "unassigned"
    eligibility: Literal["pending_metadata", "eligible"] = "pending_metadata"
    lineage_verified: bool = False
    bad_pr: PullRequestMetadata | None = None
    fix_pr: PullRequestMetadata | None = None
    reference_test: ReferenceTest | None = None
    changed_files: tuple[str, ...] = ()
    changed_symbols: tuple[str, ...] = ()
    fixed_at: AwareDatetime | None = None
    publication_evidence: tuple[PublicationEvidence, ...] = ()
    env: CaseEnvironment | None = None
    validation: CaseValidation = Field(default_factory=CaseValidation)
    human_review: HumanReview | None = None
    review_history: tuple[HumanReview, ...] = ()
    agent_review: AgentReview | None = None
    agent_review_history: tuple[AgentReview, ...] = ()
    bad_diff: CaseArtifact | None = None
    historical_fix_diff: CaseArtifact | None = None
    candidate_artifact: CaseArtifact | None = None
    notes: str = ""

    @field_validator("regression_group_id")
    @classmethod
    def validate_group(cls, value: str | None) -> str | None:
        return None if value is None else _nonblank(value)

    @field_validator("changed_files")
    @classmethod
    def validate_files(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for path in value:
            _relative_path(path)
        return value

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

    def review_input_digest(self) -> str:
        """Bind reviewed regression inputs and report bytes, excluding mutable review state."""
        fields = {
            "id",
            "repo",
            "base_commit",
            "bad_commit",
            "fix_commit",
            "repair",
            "changed_lines",
            "regression_group_id",
            "reference_test",
            "env",
            "bad_pr",
            "fix_pr",
            "bad_diff",
            "historical_fix_diff",
            "candidate_artifact",
            "eligibility",
            "lineage_verified",
            "fixed_at",
            "publication_evidence",
            "changed_files",
            "changed_symbols",
            "source_hashes",
            "global_suite_evidence",
        }
        payload = self.model_dump(mode="json", include=fields)
        if self.repair_derivation is not None:
            payload["repair_derivation"] = self.repair_derivation.model_dump(mode="json")
        payload["validation_report_sha256"] = self.validation.report_sha256
        payload["validation_implementation"] = self.validation.model_dump(
            mode="json",
            include={
                "grader_version",
                "policy_version",
                "validator_version",
                "grader_sha256",
                "policy_sha256",
            },
        )
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @model_validator(mode="after")
    def check_acceptance(self) -> Self:
        if (
            self.repair_derivation is not None
            and self.repair_derivation.derived_patch_sha256 != self.repair.patch_sha256
        ):
            raise ValueError("repair derivation must bind the scored repair patch")
        if self.human_review is not None and self.agent_review is not None:
            raise ValueError("a case can have only one active human or agent review")
        if self.validation.status != "accepted":
            return self
        review = self.human_review or self.agent_review
        if review is None or review.decision != "accept":
            raise ValueError("accepted cases require an explicit human or agent acceptance review")
        if isinstance(review, HumanReview) and not review.attested:
            raise ValueError("human acceptance requires an explicit attestation")
        if (
            not self.validation.machine_passed
            or self.validation.report is None
            or self.validation.report_sha256 is None
            or review.validation_report_sha256 != self.validation.report_sha256
        ):
            raise ValueError("acceptance review must bind the passing machine report")
        if (
            not self.changed_lines
            or self.regression_group_id is None
            or self.reference_test is None
            or self.reference_test.source_artifact is None
            or self.reference_test.source_sha256 is None
            or self.env is None
            or self.env.dependency_lock_sha256 is None
            or self.bad_diff is None
            or self.historical_fix_diff is None
        ):
            raise ValueError("accepted cases require complete regression input identities")
        if (
            self.eligibility != "eligible"
            or self.bad_pr is None
            or not self.bad_pr.association_verified
            or self.bad_pr.associated_commit != self.bad_commit
            or self.bad_pr.number is None
            or self.bad_pr.url is None
            or self.bad_pr.merged_at is None
            or self.bad_pr.observed_at is None
        ):
            raise ValueError("accepted cases require verified merged bad-PR provenance")
        pr_url = urlsplit(self.bad_pr.url)
        if (
            pr_url.scheme != "https"
            or pr_url.netloc.lower() != "github.com"
            or pr_url.path.casefold() != f"/{self.repo}/pull/{self.bad_pr.number}".casefold()
            or pr_url.query
            or pr_url.fragment
        ):
            raise ValueError("accepted bad-PR URL must match repository and PR number")
        if (
            not self.lineage_verified
            or len({self.base_commit, self.bad_commit, self.fix_commit}) != 3
        ):
            raise ValueError("accepted case inputs require verified distinct Git lineage")
        if (
            set(self.source_hashes) != {"base", "bad", "historical_fix", "repaired"}
            or self.source_hashes.get("repaired") != self.repair.repaired_tree_sha256
            or self.validation.grader_sha256 is None
            or self.validation.policy_sha256 is None
            or self.validation.grader_version is None
            or self.validation.policy_version is None
            or self.validation.validator_version is None
        ):
            raise ValueError("accepted case inputs require source and implementation identities")
        global_evidence = self.global_suite_evidence
        if (
            global_evidence is None
            or global_evidence.applies_to_image != self.env.image
            or global_evidence.grader_sha256 != self.validation.grader_sha256
            or global_evidence.policy_sha256 != self.validation.policy_sha256
        ):
            raise ValueError(
                "accepted case requires separately verified matching global CI evidence"
            )
        if review.case_inputs_sha256 != self.review_input_digest():
            raise ValueError("acceptance review must bind the immutable case inputs")
        return self


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
