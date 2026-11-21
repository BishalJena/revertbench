"""Reproduce a published case with its approved grader snapshot and Python base.

Run from a full-history RevertBench checkout:
    uv run python scripts/reproduce_case.py cases/CASE_ID --output artifacts/reproduction

This helper intentionally lives outside src/revertbench: dataset maintenance must
not change the package fingerprint recorded by an already accepted case.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import shutil
import signal
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any


class ReproductionError(RuntimeError):
    """A pinned input or a required reproduction check was unavailable or differed."""


def read_publication(case_dir: Path) -> dict[str, Any]:
    manifest = json.loads((case_dir / "publication-manifest.json").read_bytes())
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != "1":
        raise ReproductionError("unsupported publication manifest")
    files = manifest.get("files")
    if not isinstance(files, dict) or "case.json" not in files:
        raise ReproductionError("publication manifest has no case identity")
    for name, expected in files.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != name:
            raise ReproductionError("invalid publication artifact path")
        path = case_dir.joinpath(*relative.parts)
        if any(parent.is_symlink() for parent in (path, *path.parents[: len(relative.parts) - 1])):
            raise ReproductionError("publication artifacts cannot be links")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected["sha256"] or len(raw) != expected["bytes"]:
            raise ReproductionError("published artifact identity mismatch")
    base = manifest.get("build", {}).get("base_image", "")
    if re.fullmatch(r"python@sha256:[0-9a-f]{64}", base) is None:
        raise ReproductionError("reproduction requires an immutable official Python base")
    return manifest


def _run(command: list[str], *, cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    with subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            os.killpg(process.pid, signal.SIGINT)
            try:
                process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            raise ReproductionError("reproduction exceeded its time budget") from error
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _export_grader(repo_root: Path, commit: str, destination: Path) -> None:
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit) is None:
        raise ReproductionError("grader implementation must be an exact commit")
    destination.mkdir()
    archive_path = destination / "grader.tar"
    result = _run(
        [
            "git",
            "--no-pager",
            "--no-replace-objects",
            "-C",
            str(repo_root),
            "archive",
            "--format=tar",
            f"--output={archive_path}",
            commit,
            "--",
            "src/revertbench",
            "pyproject.toml",
            "uv.lock",
            "README.md",
        ],
        cwd=repo_root,
        timeout=20,
    )
    if result.returncode:
        raise ReproductionError("pinned grader commit unavailable; fetch full Git history")
    if archive_path.stat().st_size > 8 * 1024 * 1024:
        raise ReproductionError("pinned grader snapshot exceeds the expected size")
    with tarfile.open(archive_path) as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise ReproductionError("invalid grader snapshot path")
            if member.isdir():
                continue
            allowed = member.name in {"pyproject.toml", "uv.lock", "README.md"} or (
                name.parts[:2] == ("src", "revertbench") and name.suffix == ".py"
            )
            if not allowed or not member.isfile():
                raise ReproductionError("grader snapshot contains a nonallowlisted file")
            stream = archive.extractfile(member)
            if stream is None:
                raise ReproductionError("grader snapshot file is missing")
            target = destination.joinpath(*name.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(stream.read())
    archive_path.unlink()


def _has_explicit_acceptance(case: Any) -> bool:
    """Screen reviewer kind without importing a newer schema into an old snapshot.

    The pinned engine's Case schema and machine-report checks remain authoritative
    for every evidence binding; this outer check only controls process launch.
    """
    if not isinstance(case, dict):
        return False
    validation = case.get("validation")
    if not isinstance(validation, dict) or validation.get("status") != "accepted":
        return False
    human, agent = case.get("human_review"), case.get("agent_review")
    if (human is None) == (agent is None):
        return False
    review = human if human is not None else agent
    if not isinstance(review, dict) or review.get("decision") != "accept":
        return False
    identity = review.get("reviewer")
    if not isinstance(identity, str) or not identity.strip():
        return False
    if human is not None:
        return review.get("attested") is True and review.get("reviewer_kind", "human") == "human"
    authorization = review.get("authorization_note")
    return (
        review.get("reviewer_kind") == "agent"
        and isinstance(authorization, str)
        and bool(authorization.strip())
        and "attested" not in review
    )


def reproduce(case_dir: Path, *, output: Path, cache: Path) -> dict[str, Any]:
    """Launch an isolated host interpreter from this case's trusted grader commit."""
    case_dir, output, cache = case_dir.resolve(), output.resolve(), cache.resolve()
    read_publication(case_dir)
    case = json.loads((case_dir / "case.json").read_bytes())
    if not _has_explicit_acceptance(case):
        raise ReproductionError("only explicitly accepted cases can enter dataset reproduction")
    uv = shutil.which("uv")
    if uv is None:
        raise ReproductionError("uv is required for the pinned locked grader environment")
    output.mkdir(parents=True, exist_ok=False)
    cache.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="grader-", dir=cache) as temporary:
        snapshot = Path(temporary) / "snapshot"
        _export_grader(
            Path(__file__).resolve().parents[1],
            case["global_suite_evidence"]["implementation_commit"],
            snapshot,
        )
        result = _run(
            [
                uv,
                "run",
                "--project",
                str(snapshot),
                "--frozen",
                "python",
                str(Path(__file__).resolve()),
                str(case_dir),
                "--output",
                str(output),
                "--cache",
                str(cache),
                "--pinned-root",
                str(snapshot),
            ],
            cwd=snapshot,
            timeout=550,
        )
        (output / "driver.log").write_text(result.stdout + result.stderr)
        if result.returncode:
            raise ReproductionError("pinned case reproduction failed; inspect output/driver.log")
    evidence = json.loads((output / "reproduction.json").read_bytes())
    if not isinstance(evidence, dict):
        raise ReproductionError("invalid reproduction result")
    return evidence


def _official_suite(
    tree: Path, image: str, command: tuple[str, ...], client: Any
) -> dict[str, Any]:
    from revertbench.sandbox.agent import AgentSandbox

    if command != ("/usr/local/bin/python", "-m", "unittest", "-q"):
        raise ReproductionError("unsupported version-1 official suite command")
    with AgentSandbox(image, tree, client=client) as box:
        result = box.exec(list(command), timeout_seconds=60, output_limit_bytes=65536)
    stderr = result.stderr.decode("utf-8", errors="replace")
    counts = re.findall(r"^Ran (\d+) tests? in [0-9.]+s$", stderr, re.MULTILINE)
    if (
        result.status != "completed"
        or result.exit_code != 0
        or len(counts) != 1
        or re.search(r"^OK$", stderr, re.MULTILINE) is None
    ):
        raise ReproductionError("official unittest suite did not pass cleanly")
    return {
        "status": "passed",
        "tests_run": int(counts[0]),
        "exit_code": result.exit_code,
        "stdout": result.stdout.decode("utf-8", errors="replace"),
        "stderr": stderr,
    }


def _inside_pinned_grader(case_dir: Path, output: Path, cache: Path, pinned_root: Path) -> None:
    import docker

    import revertbench
    from revertbench.build import materialize_candidate
    from revertbench.mine.git import GitRepository
    from revertbench.mine.models import candidate_from_json
    from revertbench.review import _machine_passed, build_review_packet, load_case
    from revertbench.sandbox import image as image_module
    from revertbench.validate import validate_built_candidate
    from revertbench.validate.identity import host_code_identity

    if Path(revertbench.__file__).resolve().parent != pinned_root.resolve() / "src/revertbench":
        raise ReproductionError("host grading imported a different package snapshot")
    publication = read_publication(case_dir)
    case = load_case(case_dir / "case.json")
    build_review_packet(case_dir / "case.json")
    _machine_passed(case, case_dir)
    if case.env is None or case.candidate_artifact is None or case.reference_test is None:
        raise ReproductionError("accepted case lacks reproducible inputs")
    expected = (case.validation.grader_sha256, case.validation.policy_sha256)
    if host_code_identity() != expected:
        raise ReproductionError("pinned package fingerprints differ from the approved case")
    if (
        hashlib.sha256((pinned_root / "uv.lock").read_bytes()).hexdigest()
        != case.env.dependency_lock_sha256
    ):
        raise ReproductionError("pinned dependency lock differs from the approved case")
    candidate = candidate_from_json((case_dir / case.candidate_artifact.path).read_text())
    repository = GitRepository.clone(case.repo, cache / "repos")
    original_report = json.loads((case_dir / "validation.json").read_bytes())
    original_official = json.loads((case_dir / "official-unittest/summary.json").read_bytes())
    image_module._BASE_IMAGE = publication["build"]["base_image"]
    # Quiet official suites can emit nothing until completion. The transport
    # timeout must exceed their independent 60-second command wall budget.
    client = docker.from_env(timeout=70)
    try:
        client.ping()
        image = image_module.build_worker_image(client)
        with TemporaryDirectory(prefix="trees-", dir=cache) as temporary:
            built = materialize_candidate(repository, candidate, Path(temporary) / "case")
            if (
                built.case_id != case.id
                or built.source_hashes != case.source_hashes
                or built.changed_lines != case.changed_lines
                or built.patch_sha256 != case.repair.patch_sha256
                or hashlib.sha256(built.reference_source.encode()).hexdigest()
                != case.reference_test.source_sha256
            ):
                raise ReproductionError("reconstructed immutable case inputs differ")
            kwargs: dict[str, Any] = {"image": image, "client": client}
            profile = original_report.get("baseline_profile", "pytest")
            if "baseline_profile" in inspect.signature(validate_built_candidate).parameters:
                kwargs["baseline_profile"] = profile
            elif profile != "pytest":
                raise ReproductionError(
                    "pinned grader does not support the recorded baseline profile"
                )
            fresh = validate_built_candidate(built, **kwargs)
            (output / "validation.json").write_text(fresh.model_dump_json(indent=2) + "\n")
            if (
                fresh.status != "machine_validated"
                or fresh.reference is None
                or fresh.reference.score != 1
                or fresh.environment_python != case.env.python
                or fresh.dependency_lock_sha256 != case.env.dependency_lock_sha256
                or (fresh.grader_sha256, fresh.policy_sha256) != expected
                or fresh.source_hashes != case.source_hashes
                or fresh.patch_sha256 != case.repair.patch_sha256
                or fresh.reference_sha256 != case.reference_test.source_sha256
            ):
                raise ReproductionError(
                    "fresh validation did not reproduce approved identities and behavior"
                )
            official = {}
            for role, tree in (
                ("base", built.base_tree),
                ("bad", built.bad_tree),
                ("repaired", built.repaired_tree),
            ):
                official[role] = _official_suite(tree, image, case.env.test_cmd, client)
                if official[role]["tests_run"] != original_official["roles"][role]["tests_run"]:
                    raise ReproductionError("official suite count differs from approved evidence")
            result = {
                "reproduction_version": "1",
                "status": "reproduced",
                "case_id": case.id,
                "implementation_commit": case.global_suite_evidence.implementation_commit
                if case.global_suite_evidence
                else None,
                "approved_image": case.env.image,
                "fresh_image": image,
                "fresh_architecture": client.images.get(image).attrs.get("Architecture"),
                "base_image": image_module._BASE_IMAGE,
                "python": fresh.environment_python,
                "dependency_lock_sha256": fresh.dependency_lock_sha256,
                "grader_sha256": fresh.grader_sha256,
                "policy_sha256": fresh.policy_sha256,
                "source_hashes": fresh.source_hashes,
                "baseline_profile": profile,
                "reference_score": fresh.reference.score,
                "official_command": case.env.test_cmd,
                "official_suites": official,
                "approved_case_sha256": hashlib.sha256(
                    (case_dir / "case.json").read_bytes()
                ).hexdigest(),
            }
            (output / "reproduction.json").write_text(json.dumps(result, indent=2) + "\n")
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=Path("cache/case-reproduction"))
    parser.add_argument("--pinned-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.pinned_root is None:
        result = reproduce(args.case_dir, output=args.output, cache=args.cache)
        print(json.dumps({key: result[key] for key in ("status", "case_id", "fresh_image")}))
    else:
        _inside_pinned_grader(args.case_dir, args.output, args.cache, args.pinned_root)


if __name__ == "__main__":
    main()
