"""Source-only mining, controlled Docker validation and explicit case review."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

if TYPE_CHECKING:
    from revertbench.mine.git import GitRepository

import typer

from revertbench import __version__
from revertbench.review import (
    ReviewError,
    build_review_packet,
    load_case,
    read_artifact,
    record_review,
    render_review_packet,
    write_case,
)
from revertbench.schema import (
    Case,
    CaseArtifact,
    CaseEnvironment,
    CaseValidation,
    ReferenceTest,
    RepairSpec,
)

app = typer.Typer(help="RevertBench: mine, build, validate and review regression cases.")


@app.callback()
def main() -> None:
    """RevertBench command line."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


def _repository(repo: str, cache_dir: Path, local_repo: Path | None) -> "GitRepository":
    from revertbench.mine.git import GitRepository

    return (
        GitRepository(local_repo, repo=repo)
        if local_repo is not None
        else GitRepository.clone(repo, cache_dir)
    )


def _mine_candidates(
    *,
    repo: str,
    cache_dir: Path,
    output_dir: Path,
    local_repo: Path | None,
    limit: int,
    max_commits: int,
) -> int:
    from revertbench.mine.candidates import discover_report
    from revertbench.mine.models import candidate_to_json

    repository = _repository(repo, cache_dir, local_repo)
    report = discover_report(repository, limit=limit, max_commits=max_commits)
    candidates = report.candidates
    output_dir.mkdir(parents=True, exist_ok=True)
    for candidate in candidates:
        suffix = hashlib.sha256(candidate.reference_node_id.encode()).hexdigest()[:12]
        destination = output_dir / f"{candidate.fix_commit[:12]}-{suffix}.json"
        if destination.exists() or destination.is_symlink():
            raise ValueError("candidate output already exists")
        destination.write_text(candidate_to_json(candidate), encoding="utf-8")
    funnel = {
        "discovery_version": "1",
        "repo": repo,
        "examined_commits": report.examined_commits,
        "matched_fixes": report.matched_fixes,
        "candidate_count": len(candidates),
        "rejection_count": len(report.rejections),
        "rejections": [
            {"commit": commit, "reason": reason[:512], "reason_truncated": len(reason) > 512}
            for commit, reason in report.rejections[:10000]
        ],
        "rejections_truncated": len(report.rejections) > 10000,
    }
    _save_artifact(
        output_dir, "discovery.json", json.dumps(funnel, ensure_ascii=True, indent=2).encode()
    )
    return len(candidates)


@app.command()
def mine(
    repo: Annotated[str, typer.Argument(help="Public owner/repository slug.")],
    cache_dir: Annotated[Path, typer.Option()] = Path("repos"),
    output_dir: Annotated[Path, typer.Option()] = Path("artifacts/candidates"),
    local_repo: Annotated[Path | None, typer.Option()] = None,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 20,
    max_commits: Annotated[int, typer.Option(min=1, max=10000)] = 1000,
) -> None:
    """Inspect Git objects and write candidates; repository code is never executed."""
    try:
        count = _mine_candidates(
            repo=repo,
            cache_dir=cache_dir,
            output_dir=output_dir,
            local_repo=local_repo,
            limit=limit,
            max_commits=max_commits,
        )
    except (OSError, ValueError, RuntimeError) as error:
        raise typer.BadParameter(
            "mining failed; inspect repository access and supported candidate scope"
        ) from error
    typer.echo(f"Wrote {count} candidate(s).")


def _candidate_document(path: Path) -> str:
    try:
        case = load_case(path)
    except ReviewError:
        return read_artifact(path.parent, path.name, "candidate", limit=1024 * 1024).decode("utf-8")
    artifact = case.candidate_artifact
    if artifact is None:
        raise ValueError("case has no original candidate artifact")
    raw = read_artifact(path.parent, artifact.path, "candidate", limit=1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != artifact.sha256:
        raise ValueError("candidate identity mismatch")
    return raw.decode("utf-8")


def _save_artifact(root: Path, name: str, raw: bytes) -> CaseArtifact:
    destination = root / name
    if destination.is_symlink():
        raise ValueError("artifact destination cannot be a link")
    # A fresh packet directory is exclusively created by this invocation.
    with destination.open("xb") as output:
        output.write(raw)
    return CaseArtifact(path=name, sha256=hashlib.sha256(raw).hexdigest())


def _process_candidate(
    *,
    candidate_path: Path,
    cache_dir: Path,
    work_dir: Path,
    output_dir: Path,
    local_repo: Path | None,
    image: str | None,
    good_commit: str | None,
    validate: bool,
    baseline_profile: Literal["pytest", "unittest"] = "pytest",
    baseline_pids_limit: int = 64,
) -> Path:
    from dataclasses import replace
    from tempfile import TemporaryDirectory

    from revertbench.build import materialize_candidate
    from revertbench.mine.git import find_regression_boundary
    from revertbench.mine.models import candidate_from_json, candidate_to_json
    from revertbench.validate import DockerReferenceProbe

    try:
        previous_case = load_case(candidate_path)
    except ReviewError:
        previous_case = None
    candidate = candidate_from_json(_candidate_document(candidate_path))
    repository = _repository(candidate.repo, cache_dir, local_repo)
    if candidate.base_commit is None or candidate.bad_commit is None:
        if good_commit is None or image is None:
            raise ValueError(
                "candidate needs --good-commit and immutable --image for Docker history probes"
            )
        probe = DockerReferenceProbe(image)
        work_dir.mkdir(parents=True, exist_ok=True)

        def classify(commit: str) -> str:
            with TemporaryDirectory(prefix="revertbench-probe-", dir=work_dir) as temporary:
                tree = repository.export_tree(commit, Path(temporary) / "tree")
                return probe.classify(tree, candidate.reference_source)

        boundary = find_regression_boundary(repository, good_commit, candidate.fix_parent, classify)
        candidate = replace(
            candidate, base_commit=boundary.base_commit, bad_commit=boundary.bad_commit
        )
    work_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="revertbench-build-", dir=work_dir) as temporary:
        built = materialize_candidate(repository, candidate, Path(temporary) / "trees")
        root = output_dir / built.case_id
        root.mkdir(parents=True, exist_ok=False)
        repair = _save_artifact(root, "repair.patch", built.production_patch.encode("utf-8"))
        reference = _save_artifact(root, "reference.py", built.reference_source.encode("utf-8"))
        bad_diff = _save_artifact(
            root, "bad.diff", repository.diff(built.base_commit, built.bad_commit).encode("utf-8")
        )
        historical = _save_artifact(
            root,
            "historical_fix.diff",
            repository.diff(built.fix_parent, built.fix_commit).encode("utf-8"),
        )
        if built.repair_derivation is not None:
            if built.historical_production_diff is None:
                raise ValueError("derived repair lacks historical production diff")
            original_production = _save_artifact(
                root,
                built.repair_derivation.historical_diff.path,
                built.historical_production_diff.encode("utf-8"),
            )
            if original_production != built.repair_derivation.historical_diff:
                raise ValueError("historical production artifact identity differs")
        original = _save_artifact(
            root, "candidate.json", candidate_to_json(candidate).encode("utf-8")
        )
        if candidate.license_text is not None:
            _save_artifact(root, "upstream-license.txt", candidate.license_text.encode("utf-8"))
        if candidate.reference_header is not None:
            _save_artifact(
                root, "upstream-reference-header.txt", candidate.reference_header.encode("utf-8")
            )
        case = Case(
            id=built.case_id,
            repo=built.repo,
            base_commit=built.base_commit,
            bad_commit=built.bad_commit,
            fix_commit=built.fix_commit,
            repair=RepairSpec(
                patch_path=repair.path,
                patch_sha256=repair.sha256,
                repaired_tree_sha256=built.source_hashes["repaired"],
            ),
            repair_derivation=built.repair_derivation,
            changed_lines=built.changed_lines,
            source_hashes=built.source_hashes,
            changed_files=tuple(sorted(built.changed_lines)),
            regression_group_id=f"{built.repo}:{built.bad_commit}",
            lineage_verified=True,
            reference_test=ReferenceTest(
                path=built.reference_path,
                node_ids=(built.reference_node_id,),
                source_artifact=reference.path,
                source_sha256=reference.sha256,
            ),
            bad_diff=bad_diff,
            historical_fix_diff=historical,
            candidate_artifact=original,
            fixed_at=None
            if candidate.committed_at is None
            else datetime.fromisoformat(candidate.committed_at.replace("Z", "+00:00")),
            notes=(
                f"Reference derivation: {candidate.reference_derivation}; "
                "upstream notices are retained separately. PR metadata remains unverified."
            ),
        )
        if previous_case is not None:
            if (
                previous_case.repo,
                previous_case.base_commit,
                previous_case.bad_commit,
                previous_case.fix_commit,
                previous_case.repair.patch_sha256,
            ) != (
                case.repo,
                case.base_commit,
                case.bad_commit,
                case.fix_commit,
                case.repair.patch_sha256,
            ):
                raise ValueError("original case does not match reconstructed candidate identities")
            old_reference = previous_case.reference_test
            if old_reference is None or old_reference.source_sha256 != reference.sha256:
                raise ValueError("original reference identity does not match reconstruction")
            if previous_case.repair_derivation != case.repair_derivation:
                raise ValueError("original repair derivation does not match reconstruction")
            data = case.model_dump(mode="python")
            for field in (
                "bad_pr",
                "fix_pr",
                "publication_evidence",
                "dataset_split",
                "regression_group_id",
                "fixed_at",
                "changed_symbols",
                "notes",
                "eligibility",
                "review_history",
                "agent_review_history",
                "global_suite_evidence",
            ):
                data[field] = getattr(previous_case, field)
            # Old decisions remain historical; the new report requires a fresh decision.
            if (
                previous_case.human_review is not None
                and previous_case.human_review not in previous_case.review_history
            ):
                data["review_history"] = (*previous_case.review_history, previous_case.human_review)
            if (
                previous_case.agent_review is not None
                and previous_case.agent_review not in previous_case.agent_review_history
            ):
                data["agent_review_history"] = (
                    *previous_case.agent_review_history,
                    previous_case.agent_review,
                )
            data["human_review"] = None
            data["agent_review"] = None
            case = Case.model_validate(data)
        if validate:
            if image is None:
                raise ValueError("validation requires an immutable --image")
            from revertbench.validate import validate_built_candidate

            report = validate_built_candidate(
                built,
                image=image,
                baseline_profile=baseline_profile,
                baseline_pids_limit=baseline_pids_limit,
            )
            report_artifact = _save_artifact(
                root, "validation.json", report.model_dump_json(indent=2).encode("utf-8")
            )
            status = "pending_review" if report.status == "machine_validated" else report.status
            data = case.model_dump(mode="python")
            data["validation"] = CaseValidation(
                status=status,
                report=report_artifact.path,
                report_sha256=report_artifact.sha256,
                machine_passed=report.status == "machine_validated",
                reason=report.reason,
                grader_version=report.grader_version,
                policy_version=report.policy_version,
                validator_version=report.validator_version,
                grader_sha256=report.grader_sha256,
                policy_sha256=report.policy_sha256,
            )
            if report.environment_python is not None and report.dependency_lock_sha256 is not None:
                data["env"] = CaseEnvironment(
                    image=report.image,
                    python=report.environment_python,
                    dependency_lock_sha256=report.dependency_lock_sha256,
                    test_cmd=report.baseline_command,
                )
            case = Case.model_validate(data)
        destination = root / "case.json"
        write_case(case, destination)
    return destination


def _pipeline_command(
    candidate: Path,
    cache_dir: Path,
    work_dir: Path,
    output_dir: Path,
    local_repo: Path | None,
    image: str | None,
    good_commit: str | None,
    *,
    validate: bool,
    baseline_profile: Literal["pytest", "unittest"] = "pytest",
    baseline_pids_limit: int = 64,
) -> None:
    try:
        destination = _process_candidate(
            candidate_path=candidate,
            cache_dir=cache_dir,
            work_dir=work_dir,
            output_dir=output_dir,
            local_repo=local_repo,
            image=image,
            good_commit=good_commit,
            validate=validate,
            baseline_profile=baseline_profile,
            baseline_pids_limit=baseline_pids_limit,
        )
    except (OSError, ValueError, RuntimeError) as error:
        raise typer.BadParameter(
            "case processing failed; check pins, supported scope, Docker image and output path"
        ) from error
    # Paths are explicit user inputs; escape terminal controls just as review content does.
    typer.echo(json.dumps(str(destination), ensure_ascii=True))


@app.command()
def build(
    candidate: Annotated[Path, typer.Argument()],
    cache_dir: Annotated[Path, typer.Option()] = Path("repos"),
    work_dir: Annotated[Path, typer.Option()] = Path("workdir"),
    output_dir: Annotated[Path, typer.Option()] = Path("artifacts/cases"),
    local_repo: Annotated[Path | None, typer.Option()] = None,
    image: Annotated[str | None, typer.Option()] = None,
    good_commit: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Materialize pinned trees and a review packet; optional Docker history probes."""
    _pipeline_command(
        candidate, cache_dir, work_dir, output_dir, local_repo, image, good_commit, validate=False
    )


@app.command()
def validate(
    candidate: Annotated[Path, typer.Argument()],
    image: Annotated[str, typer.Option(help="Immutable worker image content identity.")],
    cache_dir: Annotated[Path, typer.Option()] = Path("repos"),
    work_dir: Annotated[Path, typer.Option()] = Path("workdir"),
    output_dir: Annotated[Path, typer.Option()] = Path("artifacts/validated-cases"),
    local_repo: Annotated[Path | None, typer.Option()] = None,
    good_commit: Annotated[str | None, typer.Option()] = None,
    baseline_profile: Annotated[
        Literal["pytest", "unittest"],
        typer.Option(
            help="Fixed upstream suite profile; unittest includes load_tests and doctests."
        ),
    ] = "pytest",
    baseline_pids_limit: Annotated[
        int,
        typer.Option(
            min=8,
            max=256,
            help=(
                "PID capacity for trusted existing suites only; "
                "scored submissions retain their limits."
            ),
        ),
    ] = 64,
) -> None:
    """Prove baseline, reference and adversarial checks in Docker; retain explicit review."""
    _pipeline_command(
        candidate,
        cache_dir,
        work_dir,
        output_dir,
        local_repo,
        image,
        good_commit,
        validate=True,
        baseline_profile=baseline_profile,
        baseline_pids_limit=baseline_pids_limit,
    )


@app.command()
def review(
    case: Annotated[Path, typer.Argument()],
    show_only: Annotated[bool, typer.Option()] = False,
    accept: Annotated[bool, typer.Option()] = False,
    reject: Annotated[bool, typer.Option()] = False,
    reviewer: Annotated[str | None, typer.Option()] = None,
    reviewer_kind: Annotated[Literal["human", "agent"], typer.Option()] = "human",
    authorization_note: Annotated[str | None, typer.Option()] = None,
    notes: Annotated[str | None, typer.Option()] = None,
    attest_human_review: Annotated[bool, typer.Option()] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show evidence by default; explicitly record a human or authorized agent review."""
    if (accept and reject) or (show_only and (accept or reject or notes is not None)):
        raise typer.BadParameter("choose one decision or --show-only")
    if reviewer_kind == "agent" and attest_human_review:
        raise typer.BadParameter("agent review cannot use --attest-human-review")
    if reviewer_kind == "human" and authorization_note is not None:
        raise typer.BadParameter("--authorization-note requires --reviewer-kind agent")
    if (
        reviewer_kind == "agent"
        and (accept or reject or notes is not None)
        and (not authorization_note or not authorization_note.strip())
    ):
        raise typer.BadParameter("agent decisions require --authorization-note")
    if reviewer_kind == "human" and accept and not attest_human_review:
        raise typer.BadParameter("--accept requires --attest-human-review")
    if (accept or reject or notes is not None) and (not reviewer or not notes):
        raise typer.BadParameter("a decision requires --reviewer and --notes")
    try:
        if accept or reject or notes is not None:
            assert reviewer is not None and notes is not None
            record_review(
                case,
                decision="accept" if accept else "reject" if reject else "notes",
                reviewer=reviewer,
                notes=notes,
                attested=attest_human_review,
                reviewer_kind=reviewer_kind,
                authorization_note=authorization_note,
            )
        packet = build_review_packet(case)
        typer.echo(
            packet.model_dump_json(indent=2) if json_output else render_review_packet(packet)
        )
    except ReviewError as error:
        raise typer.BadParameter(str(error)) from error
