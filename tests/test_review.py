"""Review packets are read-only by default and cannot preview unrelated host files."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from revertbench.review import (
    ReviewError,
    build_review_packet,
    load_case,
    record_review,
    render_review_packet,
    write_case,
)
from revertbench.schema import (
    Case,
    CaseArtifact,
    CaseEnvironment,
    CaseValidation,
    GlobalSuiteEvidence,
    PullRequestMetadata,
    ReferenceTest,
    RepairSpec,
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _packet(tmp_path: Path) -> Path:
    files = {
        "bad.diff": b"-return value.strip()\n+return value\n",
        "historical_fix.diff": b"-return value\n+return value.strip()\n",
        "repair.patch": b"-return value\n+return value.strip()\n",
        "reference.py": b"def test_regression():\n    assert True\n",
        "validation.json": json.dumps(
            {
                "status": "machine_validated",
                "reference": {"status": "passed", "score": 1, "reason": "passed"},
                "case_id": "toy",
                "reason": "all_machine_checks_passed",
                "image": "sha256:" + "b" * 64,
                "environment_python": "3.12.15",
                "dependency_lock_sha256": "c" * 64,
                "patch_sha256": _digest(b"-return value\n+return value.strip()\n"),
                "reference_sha256": _digest(b"def test_regression():\n    assert True\n"),
                "source_hashes": {
                    role: "a" * 64 for role in ("base", "bad", "historical_fix", "repaired")
                },
                "grader_sha256": "6" * 64,
                "policy_sha256": "7" * 64,
                "grader_version": "0.1.0",
                "policy_version": "1",
                "validator_version": "1",
                "baseline_reports": {
                    role: {"status": "passed"} for role in ("base", "bad", "repaired")
                },
                "probes": {
                    name: {"status": "passed", "reason": "checked"}
                    for name in (
                        "empty",
                        "assert_true",
                        "assert_false",
                        "import_error",
                        "fix_only",
                        "stability",
                        "config_tamper",
                    )
                },
            }
        ).encode(),
    }
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    case = Case(
        id="toy",
        repo="owner/toy",
        base_commit="1" * 40,
        bad_commit="2" * 40,
        fix_commit="3" * 40,
        regression_group_id="toy-regression",
        repair=RepairSpec(
            patch_path="repair.patch",
            patch_sha256=_digest(files["repair.patch"]),
            repaired_tree_sha256="a" * 64,
        ),
        changed_lines={"toy.py": (2,)},
        source_hashes={role: "a" * 64 for role in ("base", "bad", "historical_fix", "repaired")},
        global_suite_evidence=GlobalSuiteEvidence(
            ci_url="https://github.com/owner/revertbench/actions/runs/1",
            implementation_commit="5" * 40,
            applies_to_image="sha256:" + "b" * 64,
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
            source_sha256=_digest(files["reference.py"]),
        ),
        env=CaseEnvironment(
            image="sha256:" + "b" * 64, python="3.12.15", dependency_lock_sha256="c" * 64
        ),
        validation=CaseValidation(
            status="pending_human_review",
            machine_passed=True,
            report="validation.json",
            report_sha256=_digest(files["validation.json"]),
            grader_version="0.1.0",
            policy_version="1",
            validator_version="1",
            grader_sha256="6" * 64,
            policy_sha256="7" * 64,
        ),
        bad_diff=CaseArtifact(path="bad.diff", sha256=_digest(files["bad.diff"])),
        historical_fix_diff=CaseArtifact(
            path="historical_fix.diff", sha256=_digest(files["historical_fix.diff"])
        ),
    )
    path = tmp_path / "case.json"
    write_case(case, path)
    return path


def test_show_packet_is_versioned_and_does_not_accept_or_mutate_case(tmp_path: Path) -> None:
    path = _packet(tmp_path)
    before = path.read_bytes()
    packet = build_review_packet(path)
    output = render_review_packet(packet)
    assert packet.review_version == "1"
    assert "Bad change" in output
    assert "Historical fix" in output
    assert "Minimal repair" in output
    assert "Reference test" in output
    assert "pending_human_review" in output
    assert path.read_bytes() == before
    assert load_case(path).human_review is None


@pytest.mark.parametrize("attested", [False, True])
def test_acceptance_requires_explicit_attestation_and_preserves_evidence(
    tmp_path: Path, attested: bool
) -> None:
    path = _packet(tmp_path)
    if not attested:
        with pytest.raises(ReviewError, match="attest"):
            record_review(
                path,
                decision="accept",
                reviewer="Reviewer",
                notes="Inspected source, repair and reference.",
            )
        assert load_case(path).validation.status == "pending_human_review"
    else:
        reviewed = record_review(
            path,
            decision="accept",
            reviewer="Reviewer",
            notes="Inspected source, repair and reference.",
            attested=True,
        )
        assert reviewed.validation.status == "accepted"
        assert reviewed.human_review is not None
        assert reviewed.human_review.validation_report_sha256 == reviewed.validation.report_sha256
        assert reviewed.human_review.case_inputs_sha256 == reviewed.review_input_digest()
        assert load_case(path) == reviewed


def test_notes_do_not_replace_an_existing_acceptance(tmp_path: Path) -> None:
    path = _packet(tmp_path)
    accepted = record_review(
        path,
        decision="accept",
        reviewer="Reviewer",
        notes="Inspected source, repair and reference.",
        attested=True,
    )
    noted = record_review(
        path, decision="notes", reviewer="Reviewer", notes="A follow-up observation."
    )
    assert noted.validation.status == "accepted"
    assert noted.human_review == accepted.human_review
    assert len(noted.review_history) == 2


def test_reject_is_an_explicit_record_with_identity_and_notes(tmp_path: Path) -> None:
    path = _packet(tmp_path)
    case = record_review(
        path, decision="reject", reviewer="Reviewer", notes="The test checks unrelated behavior."
    )
    assert case.validation.status == "rejected"
    assert case.human_review is not None and case.human_review.decision == "reject"


@pytest.mark.parametrize(
    "artifact", [".env", "context/private.patch", "configs/models.toml", "../outside.patch"]
)
def test_untrusted_case_cannot_preview_private_or_escaping_artifacts(
    tmp_path: Path, artifact: str
) -> None:
    path = _packet(tmp_path)
    secret = b"SYNTHETIC_PRIVATE_SENTINEL"
    (tmp_path / ".env").write_bytes(secret)
    data = json.loads(path.read_text())
    data["repair"]["patch_path"] = artifact
    data["repair"]["patch_sha256"] = _digest(secret)
    path.write_text(json.dumps(data))
    with pytest.raises(ReviewError) as error:
        build_review_packet(path)
    assert secret.decode() not in str(error.value)


@pytest.mark.parametrize("link_kind", ["file", "parent", "hardlink"])
def test_artifact_links_cannot_redirect_review_reads(tmp_path: Path, link_kind: str) -> None:
    path = _packet(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    data = json.loads(path.read_text())
    if link_kind == "parent":
        (outside / "repair.patch").write_bytes((tmp_path / "repair.patch").read_bytes())
        (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
        data["repair"]["patch_path"] = "linked/repair.patch"
    elif link_kind == "hardlink":
        (tmp_path / "linked.patch").hardlink_to(tmp_path / "repair.patch")
        data["repair"]["patch_path"] = "linked.patch"
    else:
        (tmp_path / "linked.patch").symlink_to(tmp_path / "repair.patch")
        data["repair"]["patch_path"] = "linked.patch"
    path.write_text(json.dumps(data))
    with pytest.raises(ReviewError):
        build_review_packet(path)


def test_preview_output_escapes_terminal_control_sequences(tmp_path: Path) -> None:
    path = _packet(tmp_path)
    payload = b"normal\n\tline\x1b]52;c;CONTROL\x07\rhidden\x9b"
    # Keep the source UTF-8; C1 controls are represented as valid Unicode bytes.
    payload = payload[:-1] + "\u009b".encode()
    (tmp_path / "bad.diff").write_bytes(payload)
    data = json.loads(path.read_text())
    data["bad_diff"]["sha256"] = _digest(payload)
    path.write_text(json.dumps(data))
    output = render_review_packet(build_review_packet(path))
    assert (
        "\x1b" not in output
        and "\x07" not in output
        and "\r" not in output
        and "\u009b" not in output
    )
    assert "\\x1b" in output and "\\r" in output
    assert "normal\n\tline" in output


def test_preview_is_bounded_and_changed_artifacts_fail_digest_check(tmp_path: Path) -> None:
    path = _packet(tmp_path)
    packet = build_review_packet(path, max_preview_bytes=10)
    assert any(section.truncated for section in packet.sections)
    (tmp_path / "repair.patch").write_text("changed after machine validation")
    with pytest.raises(ReviewError, match="digest"):
        record_review(
            path,
            decision="accept",
            reviewer="Reviewer",
            notes="Inspected all evidence.",
            attested=True,
        )


def test_oversized_artifact_is_rejected_before_preview(tmp_path: Path) -> None:
    path = _packet(tmp_path)
    (tmp_path / "repair.patch").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    with pytest.raises(ReviewError, match="size"):
        build_review_packet(path)


def test_invalid_case_json_errors_never_echo_raw_input(tmp_path: Path) -> None:
    path = tmp_path / "case.json"
    secret = "SYNTHETIC_MALFORMED_VALUE"
    path.write_text(json.dumps({"id": secret}))
    with pytest.raises(ReviewError) as error:
        load_case(path)
    assert secret not in str(error.value)


def test_rejected_machine_report_cannot_be_overridden_by_case_flag(tmp_path: Path) -> None:
    path = _packet(tmp_path)
    report = json.loads((tmp_path / "validation.json").read_text())
    report["status"] = "rejected"
    raw = json.dumps(report).encode()
    (tmp_path / "validation.json").write_bytes(raw)
    data = json.loads(path.read_text())
    data["validation"]["report_sha256"] = _digest(raw)
    path.write_text(json.dumps(data))
    with pytest.raises(ReviewError, match="machine"):
        record_review(
            path,
            decision="accept",
            reviewer="Reviewer",
            notes="Inspected all evidence.",
            attested=True,
        )


@pytest.mark.parametrize("mutation", ["identity", "missing_probe", "extra", "boolean_score"])
def test_acceptance_requires_complete_matching_machine_report(
    tmp_path: Path, mutation: str
) -> None:
    path = _packet(tmp_path)
    report = json.loads((tmp_path / "validation.json").read_text())
    if mutation == "identity":
        report["patch_sha256"] = "0" * 64
    elif mutation == "missing_probe":
        report["probes"].pop("empty")
    elif mutation == "extra":
        report["untrusted_new_field"] = True
    else:
        report["reference"]["score"] = True
    raw = json.dumps(report).encode()
    (tmp_path / "validation.json").write_bytes(raw)
    data = json.loads(path.read_text())
    data["validation"]["report_sha256"] = _digest(raw)
    path.write_text(json.dumps(data))
    with pytest.raises(ReviewError, match="machine"):
        record_review(
            path,
            decision="accept",
            reviewer="Reviewer",
            notes="Inspected all evidence.",
            attested=True,
        )


@pytest.mark.parametrize(
    "field,value", [("environment_python", "3.12.1"), ("dependency_lock_sha256", "0" * 64)]
)
def test_acceptance_environment_must_match_image_observed_report(
    tmp_path: Path, field: str, value: str
) -> None:
    path = _packet(tmp_path)
    report = json.loads((tmp_path / "validation.json").read_text())
    report[field] = value
    raw = json.dumps(report).encode()
    (tmp_path / "validation.json").write_bytes(raw)
    data = json.loads(path.read_text())
    data["validation"]["report_sha256"] = _digest(raw)
    path.write_text(json.dumps(data))
    with pytest.raises(ReviewError, match="machine"):
        record_review(
            path,
            decision="accept",
            reviewer="Reviewer",
            notes="Inspected all evidence.",
            attested=True,
        )


def test_review_shows_pr_observation_and_original_reference_location(tmp_path: Path) -> None:
    path = _packet(tmp_path)
    data = json.loads(path.read_text())
    data["bad_pr"]["title"] = "Regression introduction"
    data["bad_pr"]["body"] = "Currently observed PR body"
    data["bad_pr"]["body_snapshot"] = "current_observed"
    path.write_text(json.dumps(data))
    output = render_review_packet(build_review_packet(path))
    assert "https://github.com/owner/toy/pull/1" in output
    assert "current_observed" in output and "Currently observed PR body" in output
    assert "tests/test_api.py::test_regression" in output


def test_candidate_artifact_digest_is_checked_before_review(tmp_path: Path) -> None:
    path = _packet(tmp_path)
    raw = b'{"synthetic": "candidate"}'
    (tmp_path / "candidate.json").write_bytes(raw)
    data = json.loads(path.read_text())
    data["candidate_artifact"] = {"path": "candidate.json", "sha256": _digest(raw)}
    path.write_text(json.dumps(data))
    (tmp_path / "candidate.json").write_text("changed source provenance")
    with pytest.raises(ReviewError, match="digest"):
        build_review_packet(path)


@pytest.mark.parametrize(
    "mutation",
    [
        "grader_hash",
        "policy_hash",
        "grader_version",
        "policy_version",
        "source_hash",
        "extra_probe",
    ],
)
def test_acceptance_rejects_unbound_implementation_or_probe_claims(
    tmp_path: Path, mutation: str
) -> None:
    path = _packet(tmp_path)
    report = json.loads((tmp_path / "validation.json").read_text())
    if mutation == "grader_hash":
        report["grader_sha256"] = None
    elif mutation == "policy_hash":
        report["policy_sha256"] = "0" * 64
    elif mutation == "grader_version":
        report["grader_version"] = "unverified-version"
    elif mutation == "policy_version":
        report["policy_version"] = "unverified-version"
    elif mutation == "source_hash":
        report["source_hashes"]["base"] = "0" * 64
    else:
        report["probes"]["pretend_global_suite"] = {
            "status": "not_applicable",
            "reason": "not actually run",
        }
    raw = json.dumps(report).encode()
    (tmp_path / "validation.json").write_bytes(raw)
    data = json.loads(path.read_text())
    data["validation"]["report_sha256"] = _digest(raw)
    path.write_text(json.dumps(data))
    with pytest.raises(ReviewError, match="machine"):
        record_review(
            path,
            decision="accept",
            reviewer="Reviewer",
            notes="Inspected all evidence.",
            attested=True,
        )
