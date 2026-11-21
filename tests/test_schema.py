from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from revertbench.schema import (
    Case,
    CaseArtifact,
    CaseEnvironment,
    CaseValidation,
    ExecutionResult,
    GlobalSuiteEvidence,
    GradeResult,
    HumanReview,
    PublicationEvidence,
    PullRequestMetadata,
    ReferenceTest,
    RepairSpec,
)


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


def _reviewable_case() -> Case:
    return Case(
        id="toy-regression",
        repo="owner/toy",
        regression_group_id="toy-issue-1",
        base_commit="1" * 40,
        bad_commit="2" * 40,
        fix_commit="3" * 40,
        repair=RepairSpec(
            patch_path="repair.patch", patch_sha256="a" * 64, repaired_tree_sha256="b" * 64
        ),
        changed_lines={"toy.py": (2,)},
        source_hashes={role: "b" * 64 for role in ("base", "bad", "historical_fix", "repaired")},
        global_suite_evidence=GlobalSuiteEvidence(
            ci_url="https://github.com/owner/revertbench/actions/runs/1",
            implementation_commit="5" * 40,
            applies_to_image="sha256:" + "d" * 64,
            grader_sha256="6" * 64,
            policy_sha256="7" * 64,
            observed_at=datetime.now(UTC),
            checks_status="passed",
            sandbox_status="passed",
        ),
        eligibility="eligible",
        lineage_verified=True,
        bad_pr=PullRequestMetadata(
            number=1,
            url="https://github.com/owner/toy/pull/1",
            association_verified=True,
            associated_commit="2" * 40,
            merged_at=datetime.now(UTC),
            observed_at=datetime.now(UTC),
        ),
        reference_test=ReferenceTest(
            path="tests/test_api.py",
            node_ids=("tests/test_api.py::test_regression",),
            source_artifact="reference.py",
            source_sha256="c" * 64,
        ),
        env=CaseEnvironment(
            image="sha256:" + "d" * 64, python="3.12.15", dependency_lock_sha256="e" * 64
        ),
        validation=CaseValidation(
            status="pending_human_review",
            machine_passed=True,
            report="validation.json",
            report_sha256="f" * 64,
            grader_version="0.1.0",
            policy_version="1",
            validator_version="1",
            grader_sha256="6" * 64,
            policy_sha256="7" * 64,
        ),
        bad_diff=CaseArtifact(path="bad.diff", sha256="1" * 64),
        historical_fix_diff=CaseArtifact(path="historical_fix.diff", sha256="2" * 64),
    )


def test_agent_acceptance_has_separate_authorization_and_no_human_attestation() -> None:
    from revertbench.schema import AgentReview

    case = _reviewable_case()
    assert case.validation.report_sha256 is not None
    review = AgentReview(
        decision="accept",
        reviewer="Codex regression reviewer",
        authorization_note="User explicitly delegated case review.",
        notes="Inspected the original test and controlled repair.",
        reviewed_at=datetime.now(UTC),
        validation_report_sha256=case.validation.report_sha256,
        case_inputs_sha256=case.review_input_digest(),
    )
    data = case.model_dump(mode="python")
    data.update(
        validation=case.validation.model_copy(update={"status": "accepted"}), agent_review=review
    )
    accepted = Case.model_validate(data)
    assert accepted.human_review is None
    assert accepted.agent_review == review
    assert "attested" not in review.model_dump()
    assert Case.model_validate_json(accepted.model_dump_json()) == accepted
    for mutation in ({"bad_commit": "4" * 40}, {"global_suite_evidence": None}):
        with pytest.raises(ValidationError):
            Case.model_validate({**data, **mutation})
    with pytest.raises(ValidationError, match=r"one|both"):
        Case.model_validate(
            {
                **data,
                "human_review": HumanReview(
                    decision="accept",
                    reviewer="Real human",
                    notes="Reviewed",
                    reviewed_at=datetime.now(UTC),
                    attested=True,
                    validation_report_sha256=case.validation.report_sha256,
                    case_inputs_sha256=case.review_input_digest(),
                ),
            }
        )
    for field in ("validation_report_sha256", "case_inputs_sha256"):
        with pytest.raises(ValidationError, match=r"report|inputs"):
            Case.model_validate(
                {**data, "agent_review": review.model_copy(update={field: "0" * 64})}
            )


def test_agent_review_has_no_human_attestation_and_requires_aware_time() -> None:
    from revertbench.schema import AgentReview

    values = dict(
        decision="accept",
        reviewer="Codex",
        notes="Checked",
        authorization_note="User delegated review",
        reviewed_at=datetime.now(UTC),
        validation_report_sha256="a" * 64,
        case_inputs_sha256="b" * 64,
    )
    for change in ({"attested": True}, {"reviewed_at": datetime(2026, 11, 21)}):
        with pytest.raises(ValidationError):
            AgentReview.model_validate({**values, **change})


@pytest.mark.parametrize(
    "field",
    ["reviewer", "notes", "authorization_note", "validation_report_sha256", "case_inputs_sha256"],
)
def test_agent_review_requires_identity_authorization_and_bound_inputs(field: str) -> None:
    from revertbench.schema import AgentReview

    data: dict[str, object] = dict(
        decision="notes",
        reviewer="Codex",
        notes="Observed",
        authorization_note="Delegated by user",
        reviewed_at=datetime.now(UTC),
        validation_report_sha256="a" * 64,
        case_inputs_sha256="b" * 64,
    )
    data[field] = " " if field in {"reviewer", "notes", "authorization_note"} else None
    with pytest.raises(ValidationError):
        AgentReview.model_validate(data)


def test_generic_pending_review_requires_machine_evidence_and_legacy_stays_readable() -> None:
    case = _reviewable_case()
    data = case.validation.model_dump(mode="python")
    assert CaseValidation.model_validate({**data, "status": "pending_review"}).machine_passed
    assert CaseValidation.model_validate(data).status == "pending_human_review"
    with pytest.raises(ValidationError):
        CaseValidation.model_validate({**data, "status": "pending_review", "machine_passed": False})


def test_full_case_round_trips_unknown_publication_dates_and_dataset_group() -> None:
    case = _reviewable_case()
    data = case.model_dump(mode="python")
    data["publication_evidence"] = (
        PublicationEvidence(
            artifact="issue",
            url="https://github.com/owner/toy/issues/1",
            published_at=None,
            observed_at=datetime(2026, 11, 21, tzinfo=UTC),
        ),
    )
    data["fixed_at"] = datetime(2026, 11, 20, tzinfo=UTC)
    enriched = Case.model_validate(data)
    assert enriched.dataset_split == "unassigned"
    assert enriched.publication_evidence[0].published_at is None
    assert Case.model_validate_json(enriched.model_dump_json()) == enriched


@pytest.mark.parametrize("naive_field", ["published_at", "observed_at"])
def test_publication_timestamps_require_timezone(naive_field: str) -> None:
    data: dict[str, object] = {
        "artifact": "issue",
        "url": "https://github.com/owner/toy/issues/1",
        "published_at": datetime(2026, 11, 20, tzinfo=UTC),
        "observed_at": datetime(2026, 11, 21, tzinfo=UTC),
    }
    data[naive_field] = datetime(2026, 11, 21)
    with pytest.raises(ValidationError):
        PublicationEvidence.model_validate(data)


def test_fixed_at_requires_timezone_and_no_global_model_cutoff_label() -> None:
    case = _reviewable_case()
    with pytest.raises(ValidationError):
        Case.model_validate({**case.model_dump(mode="python"), "fixed_at": datetime(2026, 11, 21)})
    with pytest.raises(ValidationError):
        Case.model_validate({**case.model_dump(mode="python"), "split": "post_cutoff"})


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "https://user:secret@example.com/issue", "javascript:alert(1)"]
)
def test_publication_evidence_cannot_embed_credentials_or_local_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        PublicationEvidence(artifact="issue", url=url, observed_at=datetime.now(UTC))


@pytest.mark.parametrize(
    "path", ["../reference.py", "/tmp/reference.py", "a/../reference.py", "bad\nreference.py"]
)
def test_reference_artifact_paths_are_canonical_relative(path: str) -> None:
    with pytest.raises(ValidationError):
        ReferenceTest(
            path="tests/test_api.py",
            node_ids=("tests/test_api.py::test_regression",),
            source_artifact=path,
        )


def test_acceptance_requires_an_explicit_attested_human_review() -> None:
    case = _reviewable_case()
    data = case.model_dump(mode="python")
    data["validation"] = case.validation.model_copy(update={"status": "accepted"})
    with pytest.raises(ValidationError, match="human"):
        Case.model_validate(data)

    data["human_review"] = HumanReview(
        decision="accept",
        reviewer="human reviewer",
        notes="Inspected the regression and minimal repair.",
        reviewed_at=datetime.now(UTC),
        attested=False,
        validation_report_sha256=case.validation.report_sha256,
        case_inputs_sha256=case.review_input_digest(),
    )
    with pytest.raises(ValidationError, match="attest"):
        Case.model_validate(data)


def test_attested_acceptance_is_bound_to_report_and_immutable_case_inputs() -> None:
    case = _reviewable_case()
    review = HumanReview(
        decision="accept",
        reviewer="human reviewer",
        notes="Inspected the regression and minimal repair.",
        reviewed_at=datetime.now(UTC),
        attested=True,
        validation_report_sha256=case.validation.report_sha256,
        case_inputs_sha256=case.review_input_digest(),
    )
    data = case.model_dump(mode="python")
    data.update(
        validation=case.validation.model_copy(update={"status": "accepted"}), human_review=review
    )
    accepted = Case.model_validate(data)
    assert accepted.validation.status == "accepted"
    assert Case.model_validate_json(accepted.model_dump_json()) == accepted

    assert case.reference_test is not None
    for change in (
        {"bad_commit": "4" * 40},
        {"reference_test": case.reference_test.model_copy(update={"source_sha256": "0" * 64})},
    ):
        with pytest.raises(ValidationError, match=r"input|PR"):
            Case.model_validate({**data, **change})
    with pytest.raises(ValidationError, match="report"):
        Case.model_validate(
            {
                **data,
                "human_review": review.model_copy(update={"validation_report_sha256": "0" * 64}),
            }
        )


def test_accepted_case_cannot_use_incomplete_machine_metadata() -> None:
    case = _reviewable_case()
    review = HumanReview(
        decision="accept",
        reviewer="human reviewer",
        notes="Inspected the regression and minimal repair.",
        reviewed_at=datetime.now(UTC),
        attested=True,
        validation_report_sha256=case.validation.report_sha256,
        case_inputs_sha256=case.review_input_digest(),
    )
    data = case.model_dump(mode="python")
    data.update(
        validation=case.validation.model_copy(
            update={"status": "accepted", "machine_passed": False}
        ),
        human_review=review,
    )
    with pytest.raises(ValidationError):
        Case.model_validate(data)


@pytest.mark.parametrize("field", ["reviewer", "notes"])
def test_human_review_requires_nonblank_identity_and_notes(field: str) -> None:
    data: dict[str, object] = {
        "decision": "notes",
        "reviewer": "reviewer",
        "notes": "meaningful note",
        "reviewed_at": datetime.now(UTC),
    }
    data[field] = "   "
    with pytest.raises(ValidationError):
        HumanReview.model_validate(data)


def test_human_acceptance_cannot_substitute_for_verified_bad_pr() -> None:
    case = _reviewable_case()
    review = HumanReview(
        decision="accept",
        reviewer="Reviewer",
        notes="Inspected regression evidence.",
        reviewed_at=datetime.now(UTC),
        attested=True,
        validation_report_sha256=case.validation.report_sha256,
        case_inputs_sha256=case.review_input_digest(),
    )
    data = case.model_dump(mode="python")
    data.update(
        validation=case.validation.model_copy(update={"status": "accepted"}),
        human_review=review,
        bad_pr=None,
    )
    with pytest.raises(ValidationError, match="PR"):
        Case.model_validate(data)


@pytest.mark.parametrize("mutation", ["lineage", "duplicate", "pr_commit", "eligibility"])
def test_accepted_provenance_must_describe_actual_regression_boundary(mutation: str) -> None:
    case = _reviewable_case()
    assert case.bad_pr is not None
    data = case.model_dump(mode="python")
    if mutation == "lineage":
        data["lineage_verified"] = False
    elif mutation == "duplicate":
        data["fix_commit"] = case.bad_commit
    elif mutation == "pr_commit":
        data["bad_pr"] = case.bad_pr.model_copy(update={"associated_commit": "4" * 40})
    else:
        data["eligibility"] = "pending_metadata"
    changed = Case.model_validate(data)
    data["human_review"] = HumanReview(
        decision="accept",
        reviewer="Reviewer",
        notes="Inspected actual history.",
        reviewed_at=datetime.now(UTC),
        attested=True,
        validation_report_sha256=case.validation.report_sha256,
        case_inputs_sha256=changed.review_input_digest(),
    )
    data["validation"] = case.validation.model_copy(update={"status": "accepted"})
    with pytest.raises(ValidationError):
        Case.model_validate(data)


def test_sha256_git_commit_ids_round_trip_without_truncation() -> None:
    case = _reviewable_case()
    extended = Case.model_validate({**case.model_dump(mode="python"), "base_commit": "1" * 64})
    assert len(extended.base_commit) == 64


def test_accepted_case_requires_separate_global_suite_evidence() -> None:
    case = _reviewable_case()
    data = case.model_dump(mode="python")
    data["global_suite_evidence"] = None
    changed = Case.model_validate(data)
    data["human_review"] = HumanReview(
        decision="accept",
        reviewer="Reviewer",
        notes="Inspected all evidence.",
        reviewed_at=datetime.now(UTC),
        attested=True,
        validation_report_sha256=case.validation.report_sha256,
        case_inputs_sha256=changed.review_input_digest(),
    )
    data["validation"] = case.validation.model_copy(update={"status": "accepted"})
    with pytest.raises(ValidationError, match="global"):
        Case.model_validate(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("grader_version", "tampered"),
        ("policy_version", "tampered"),
        ("validator_version", "tampered"),
    ],
)
def test_accepted_review_digest_binds_recorded_implementation_versions(
    field: str, value: str
) -> None:
    case = _reviewable_case()
    data = case.model_dump(mode="python")
    data["human_review"] = HumanReview(
        decision="accept",
        reviewer="Reviewer",
        notes="Inspected all evidence.",
        reviewed_at=datetime.now(UTC),
        attested=True,
        validation_report_sha256=case.validation.report_sha256,
        case_inputs_sha256=case.review_input_digest(),
    )
    data["validation"] = case.validation.model_copy(update={"status": "accepted", field: value})
    with pytest.raises(ValidationError, match="immutable"):
        Case.model_validate(data)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/other/project/pull/1",
        "https://github.com/owner/toy/pull/99",
        "https://example.com/owner/toy/pull/1",
    ],
)
def test_accepted_bad_pr_url_must_match_repository_and_number(url: str) -> None:
    case = _reviewable_case()
    assert case.bad_pr is not None
    data = case.model_dump(mode="python")
    data["bad_pr"] = case.bad_pr.model_copy(update={"url": url})
    changed = Case.model_validate(data)
    data["human_review"] = HumanReview(
        decision="accept",
        reviewer="Reviewer",
        notes="Inspected all evidence.",
        reviewed_at=datetime.now(UTC),
        attested=True,
        validation_report_sha256=case.validation.report_sha256,
        case_inputs_sha256=changed.review_input_digest(),
    )
    data["validation"] = case.validation.model_copy(update={"status": "accepted"})
    with pytest.raises(ValidationError, match="PR"):
        Case.model_validate(data)
