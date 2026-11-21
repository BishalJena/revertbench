"""Bounded review packets and explicit, evidence-bound human decisions."""

import hashlib
import os
import stat
import tempfile
import unicodedata
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import ValidationError

from revertbench.schema import (
    AgentReview,
    Case,
    EvidenceModel,
    HumanReview,
    ReviewDecision,
    ReviewerKind,
)


class ReviewError(ValueError):
    """Safe diagnostic that contains no untrusted document contents."""


class ReviewSection(EvidenceModel):
    label: str
    content: str
    truncated: bool = False


class ReviewPacket(EvidenceModel):
    review_version: Literal["1"] = "1"
    case_id: str
    status: str
    case_inputs_sha256: str
    sections: tuple[ReviewSection, ...]


_LIMIT = 8 * 1024 * 1024
_SUFFIXES = {
    "case": ".json",
    "candidate": ".json",
    "report": ".json",
    "reference": ".py",
    "repair": ".patch",
    "diff": ".diff",
}
_PRIVATE = {
    "context",
    "configs",
    "credentials",
    "auth",
    "secrets",
    "private",
    "id_rsa",
    "id_ed25519",
}


def _artifact_path(name: str, role: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or path.as_posix() != name
        or ".." in path.parts
        or "\\" in name
        or len(path.parts) > 64
        or path.suffix != _SUFFIXES[role]
        or any(unicodedata.category(character).startswith("C") for character in name)
        or any(part.startswith(".") or part.lower() in _PRIVATE for part in path.parts)
        or any(
            word in path.name.lower()
            for word in ("credential", "private_key", "secret", "models.toml")
        )
    ):
        raise ReviewError("artifact path is not permitted for this review role")
    return path


def read_artifact(root: Path, name: str, role: str, *, limit: int = _LIMIT) -> bytes:
    """Read a role-constrained regular file through no-follow directory descriptors."""
    path = _artifact_path(name, role)
    descriptors: list[int] = []
    try:
        directory = os.open(root.resolve(strict=True), os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(directory)
        for component in path.parts[:-1]:
            directory = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            descriptors.append(directory)
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        descriptors.append(descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ReviewError("artifact must be an unlinked regular file")
        if metadata.st_size > limit:
            raise ReviewError("artifact size exceeds review limit")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        result = b"".join(chunks)
        if len(result) > limit:
            raise ReviewError("artifact size exceeds review limit")
        return result
    except OSError as error:
        raise ReviewError("artifact could not be read safely") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def load_case(path: Path) -> Case:
    try:
        raw = read_artifact(path.parent, path.name, "case", limit=1024 * 1024)
        return Case.model_validate_json(raw)
    except (ValidationError, UnicodeError, ValueError) as error:
        if isinstance(error, ReviewError):
            raise
        raise ReviewError("case document is invalid") from error


def write_case(case: Case, path: Path) -> None:
    """Atomically persist a strictly validated case without following an existing link."""
    try:
        validated = Case.model_validate_json(case.model_dump_json())
        _artifact_path(path.name, "case")
        if path.is_symlink():
            raise ReviewError("case destination cannot be a link")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".case-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(validated.model_dump_json(indent=2) + "\n")
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    except (OSError, ValidationError) as error:
        raise ReviewError("case document could not be written safely") from error


def _escape(value: str) -> str:
    result: list[str] = []
    for character in value:
        if character in {"\n", "\t"} or not unicodedata.category(character).startswith("C"):
            result.append(character)
        elif character == "\r":
            result.append("\\r")
        elif ord(character) < 256:
            result.append(f"\\x{ord(character):02x}")
        else:
            result.append(f"\\u{ord(character):04x}")
    return "".join(result)


def _verified(root: Path, name: str, digest: str, role: str) -> bytes:
    raw = read_artifact(root, name, role)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ReviewError("artifact digest does not match recorded evidence")
    return raw


def build_review_packet(path: Path, *, max_preview_bytes: int = 65536) -> ReviewPacket:
    if not 1 <= max_preview_bytes <= _LIMIT:
        raise ReviewError("preview size must be positive and bounded")
    case = load_case(path)
    sections: list[ReviewSection] = []
    artifacts: list[tuple[str, str, str, str]] = [
        ("Minimal repair", case.repair.patch_path, case.repair.patch_sha256, "repair")
    ]
    for label, artifact in (
        ("Bad change", case.bad_diff),
        ("Historical fix", case.historical_fix_diff),
    ):
        if artifact is not None:
            artifacts.append((label, artifact.path, artifact.sha256, "diff"))
    if case.repair_derivation is not None:
        original = case.repair_derivation.historical_diff
        artifacts.append(("Historical production repair", original.path, original.sha256, "diff"))
        sections.append(
            ReviewSection(
                label="Repair derivation",
                content=_escape(case.repair_derivation.model_dump_json(indent=2)),
            )
        )
    reference = case.reference_test
    if (
        reference is not None
        and reference.source_artifact is not None
        and reference.source_sha256 is not None
    ):
        artifacts.append(
            ("Reference test", reference.source_artifact, reference.source_sha256, "reference")
        )
    if case.validation.report is not None and case.validation.report_sha256 is not None:
        artifacts.append(
            ("Machine validation", case.validation.report, case.validation.report_sha256, "report")
        )
    if case.candidate_artifact is not None:
        original = case.candidate_artifact
        artifacts.append(
            ("Mining provenance and upstream notices", original.path, original.sha256, "candidate")
        )
    for label, name, digest, role in artifacts:
        raw = _verified(path.parent, name, digest, role)
        content = raw[:max_preview_bytes].decode("utf-8", errors="replace")
        sections.append(
            ReviewSection(
                label=label, content=_escape(content), truncated=len(raw) > max_preview_bytes
            )
        )
    metadata = (
        f"Repository: {case.repo}\nBase: {case.base_commit}\n"
        f"Bad: {case.bad_commit}\nFix: {case.fix_commit}\n"
        f"Eligibility: {case.eligibility}\n"
    )
    if reference is not None:
        metadata += "Reference nodes: " + ", ".join(reference.node_ids) + "\n"
    for label, pr in (("Bad PR", case.bad_pr), ("Fix PR", case.fix_pr)):
        if pr is not None:
            metadata += (
                f"{label}: {pr.url}\n{pr.title}\n{pr.body}\n"
                f"body_snapshot={pr.body_snapshot} observed={pr.observed_at} "
                f"updated={pr.updated_at} merged={pr.merged_at}\n"
                f"association_verified={pr.association_verified} commit={pr.associated_commit}\n"
            )
    for evidence in case.publication_evidence:
        metadata += (
            f"{evidence.artifact}: {evidence.url} "
            f"published={evidence.published_at} observed={evidence.observed_at}\n"
        )
    sections.insert(0, ReviewSection(label="Provenance", content=_escape(metadata)))
    for label, active, history in (
        ("Human review", case.human_review, case.review_history),
        ("Agent review", case.agent_review, case.agent_review_history),
    ):
        if active is not None or history:
            content = "Active decision: " + (
                "none" if active is None else active.model_dump_json(indent=2)
            )
            content += "\nHistorical records:\n" + "\n".join(
                record.model_dump_json(indent=2) for record in history
            )
            raw = content.encode("utf-8")
            sections.append(
                ReviewSection(
                    label=label,
                    content=_escape(raw[:max_preview_bytes].decode("utf-8", errors="replace")),
                    truncated=len(raw) > max_preview_bytes,
                )
            )
    return ReviewPacket(
        case_id=_escape(case.id),
        status=case.validation.status,
        case_inputs_sha256=case.review_input_digest(),
        sections=tuple(sections),
    )


def render_review_packet(packet: ReviewPacket) -> str:
    lines = [f"Case {_escape(packet.case_id)} — {_escape(packet.status)}"]
    for section in packet.sections:
        lines.extend((f"\n{_escape(section.label)}", _escape(section.content)))
        if section.truncated:
            lines.append("[preview truncated]")
    return "\n".join(lines)


def _machine_passed(case: Case, root: Path) -> None:
    if (
        not case.validation.machine_passed
        or case.validation.report is None
        or case.validation.report_sha256 is None
    ):
        raise ReviewError("acceptance requires passing machine evidence")
    raw = _verified(root, case.validation.report, case.validation.report_sha256, "report")
    if case.repair_derivation is not None:
        original = case.repair_derivation.historical_diff
        _verified(root, original.path, original.sha256, "diff")
    from revertbench.validate import MachineValidationReport

    try:
        report = MachineValidationReport.model_validate_json(raw)
    except (ValidationError, ValueError, RecursionError) as error:
        raise ReviewError("machine validation report is invalid") from error
    required = {
        "empty",
        "assert_true",
        "assert_false",
        "import_error",
        "fix_only",
        "stability",
        "config_tamper",
    }
    reference = case.reference_test
    if (
        report.status != "machine_validated"
        or report.case_id != case.id
        or report.patch_sha256 != case.repair.patch_sha256
        or report.repair_derivation_sha256
        != (None if case.repair_derivation is None else case.repair_derivation.content_digest())
        or report.source_hashes != case.source_hashes
        or report.grader_sha256 is None
        or report.policy_sha256 is None
        or report.grader_sha256 != case.validation.grader_sha256
        or report.policy_sha256 != case.validation.policy_sha256
        or report.grader_version != case.validation.grader_version
        or report.policy_version != case.validation.policy_version
        or report.validator_version != case.validation.validator_version
        or reference is None
        or report.reference_sha256 != reference.source_sha256
        or case.env is None
        or report.image != case.env.image
        or report.environment_python != case.env.python
        or report.dependency_lock_sha256 != case.env.dependency_lock_sha256
        or report.reference is None
        or report.reference.status != "passed"
        or report.reference.score != 1
        or not all(
            role in report.baseline_reports and report.baseline_reports[role].status == "passed"
            for role in ("base", "bad", "repaired")
        )
        or required != report.probes.keys()
        or any(probe.status not in {"passed", "not_applicable"} for probe in report.probes.values())
        or any(report.probes[name].status != "passed" for name in required - {"fix_only"})
    ):
        raise ReviewError("machine validation report does not establish acceptance")


def record_review(
    path: Path,
    *,
    decision: ReviewDecision,
    reviewer: str,
    notes: str,
    attested: bool = False,
    reviewer_kind: ReviewerKind = "human",
    authorization_note: str | None = None,
) -> Case:
    case = load_case(path)
    if reviewer_kind not in {"human", "agent"}:
        raise ReviewError("reviewer kind must be human or agent")
    if reviewer_kind == "agent" and (
        attested or not authorization_note or not authorization_note.strip()
    ):
        raise ReviewError("agent review requires explicit authorization and cannot attest as human")
    if reviewer_kind == "human" and authorization_note is not None:
        raise ReviewError("authorization notes apply only to agent reviews")
    if reviewer_kind == "human" and decision == "accept" and not attested:
        raise ReviewError("acceptance requires explicit human attestation")
    build_review_packet(path)
    if decision == "accept":
        _machine_passed(case, path.parent)
    try:
        data = case.model_dump(mode="python")
        values = dict(
            decision=decision,
            reviewer=reviewer,
            notes=notes,
            reviewed_at=datetime.now(UTC),
            validation_report_sha256=case.validation.report_sha256,
            case_inputs_sha256=case.review_input_digest(),
        )
        # Preserve active legacy decisions even when their optional history was absent.
        human_history, agent_history = case.review_history, case.agent_review_history
        if case.human_review is not None and case.human_review not in human_history:
            human_history = (*human_history, case.human_review)
        if case.agent_review is not None and case.agent_review not in agent_history:
            agent_history = (*agent_history, case.agent_review)
        review: HumanReview | AgentReview
        if reviewer_kind == "agent":
            review = AgentReview.model_validate(
                {**values, "authorization_note": authorization_note}
            )
            agent_history = (*agent_history, review)
        else:
            review = HumanReview.model_validate({**values, "attested": attested})
            human_history = (*human_history, review)
        data["review_history"] = human_history
        data["agent_review_history"] = agent_history
        if decision != "notes":
            data["human_review"] = review if reviewer_kind == "human" else None
            data["agent_review"] = review if reviewer_kind == "agent" else None
            validation = case.validation.model_dump(mode="python")
            validation["status"] = "accepted" if decision == "accept" else "rejected"
            data["validation"] = validation
        updated = Case.model_validate(data)
    except ValidationError as error:
        raise ReviewError(
            "review decision requires valid identity, notes and complete verified provenance"
        ) from error
    write_case(updated, path)
    return updated
