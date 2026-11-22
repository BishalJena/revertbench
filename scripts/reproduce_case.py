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
    if not isinstance(manifest, dict) or manifest.get("manifest_version") not in {"1", "2"}:
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
    case = json.loads((case_dir / "case.json").read_bytes())
    if manifest["manifest_version"] == "2":
        _v2_allowlist(case_dir, manifest, case)
    if case.get("repair_derivation") is not None:
        proof = case["repair_derivation"]
        expected = {
            "case.json",
            "candidate.json",
            "reference.py",
            "repair.patch",
            "bad.diff",
            "historical_fix.diff",
            "validation.json",
            "upstream-license.txt",
            "upstream-reference-header.txt",
            "official-unittest/base.json",
            "official-unittest/bad.json",
            "official-unittest/repaired.json",
            "official-unittest/summary.json",
            "historical-production.diff",
        }
        if manifest["manifest_version"] == "2":
            expected -= {f"official-unittest/{role}.json" for role in (*_ROLES, "summary")}
            expected |= {f"upstream-suite/{role}.json" for role in (*_ROLES, "summary")}
        historical = proof.get("historical_diff") if isinstance(proof, dict) else None
        actual = {
            path.relative_to(case_dir).as_posix()
            for path in case_dir.rglob("*")
            if path.is_file() or path.is_symlink()
        }
        if (
            not isinstance(proof, dict)
            or proof.get("version") != "historical_function_body_v1"
            or not isinstance(historical, dict)
            or historical.get("path") != "historical-production.diff"
            or set(files) != expected
            or actual != expected | {"publication-manifest.json"}
            or historical.get("sha256") != files["historical-production.diff"]["sha256"]
        ):
            raise ReproductionError("derived repair publication has an invalid artifact allowlist")
    return manifest


_ROLES = ("base", "bad", "repaired")
_BABEL_PROFILE = "babel_upstream_default_behavior_v1"
_BABEL_COLLECTION_SKIPS = (
    "tests/interop/test_jinja2_interop.py",
    "tests/messages/test_setuptools_frontend.py",
)
_UPSTREAM_PROFILES = {
    _BABEL_PROFILE,
    "blinker_upstream_default_behavior_v1",
    "boltons_upstream_default_behavior_v1",
    "pydash_upstream_default_behavior_v1",
}
_IDENTITY_FIELDS = (
    "environment_python",
    "grader_sha256",
    "policy_sha256",
    "dependency_lock_sha256",
)
_SUITE_FIELDS = (
    "collected",
    "passed",
    "failed",
    "errors",
    "skipped",
    "xfailed",
    "xpassed",
    "deselected",
    "node_ids",
    "collection_errors",
    "extra_skip_node_ids",
)


def _upstream_scope(profile: str) -> dict[str, Any]:
    if profile not in _UPSTREAM_PROFILES:
        raise ReproductionError("unsupported upstream suite profile")
    if profile == _BABEL_PROFILE:
        return {
            "behavior": "upstream_default_unit_and_package_doctests",
            "excluded_checks": ["coverage_quality_gate", "documentation_build", "package_build"],
            "collection_skips": list(_BABEL_COLLECTION_SKIPS),
            "baseline_memory_bytes": 536_870_912,
            "locale_data": {
                "release": "Babel 2.16.0",
                "sha256": "38c999568bebecfc77cb41875776beb97382dd55b3133aaaac73f6dd98c6cf5e",
            },
        }
    if profile == "blinker_upstream_default_behavior_v1":
        return {
            "behavior": "upstream_default_pytest_tests",
            "excluded_checks": ["documentation_build", "lint", "package_build", "type_checking"],
        }
    excluded = ["coverage_quality_gate", "documentation_build", "lint", "package_build"]
    if profile == "pydash_upstream_default_behavior_v1":
        excluded.insert(0, "opt_in_typing_tests")
    return {"behavior": "upstream_default_unit_and_package_doctests", "excluded_checks": excluded}


def _v2_allowlist(case_dir: Path, manifest: dict[str, Any], case: dict[str, Any]) -> None:
    expected = {
        "case.json",
        "candidate.json",
        "reference.py",
        "repair.patch",
        "bad.diff",
        "historical_fix.diff",
        "validation.json",
        "upstream-license.txt",
        "upstream-reference-header.txt",
        *(f"upstream-suite/{role}.json" for role in (*_ROLES, "summary")),
    }
    if case.get("repair_derivation") is not None:
        expected.add("historical-production.diff")
    actual = {
        path.relative_to(case_dir).as_posix()
        for path in case_dir.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if (
        set(manifest["files"]) != expected
        or actual != expected | {"publication-manifest.json"}
        or manifest.get("case_id") != case.get("id")
    ):
        raise ReproductionError("version-2 publication has an invalid artifact allowlist")
    report = json.loads((case_dir / "validation.json").read_bytes())
    # Only the later pinned interpreter may check the command registry.
    _upstream_evidence(case_dir, case, report)


def _matching_suite(
    result: dict[str, Any],
    approved: dict[str, Any],
    *,
    profile: str,
    command: list[str],
    pids: int,
    identity: dict[str, Any],
) -> None:
    nodes = result.get("node_ids")
    collection_skips = _BABEL_COLLECTION_SKIPS if profile == _BABEL_PROFILE else ()
    actual_skips = result.get("extra_skip_node_ids", [])
    counts: list[Any] = [
        result.get(key) for key in ("passed", "failed", "errors", "skipped", "xfailed", "xpassed")
    ]
    if (
        result.get("status") != "passed"
        or result.get("exit_code") != 0
        or result.get("failed") != 0
        or result.get("errors") != 0
        or result.get("collection_errors")
        or actual_skips != list(collection_skips)
        or any(type(count) is not int or count < 0 for count in counts)
        or sum(counts) - len(collection_skips) != result.get("collected")
        or counts[3] < len(collection_skips)
        or result.get("profile") != profile
        or result.get("command") != command
        or type(result.get("pids_limit")) is not int
        or result["pids_limit"] != pids
        or any(result.get(key) != identity.get(key) for key in _IDENTITY_FIELDS)
        or any(result.get(key) != approved.get(key) for key in _SUITE_FIELDS)
        or not isinstance(nodes, list)
        or not nodes
        or any(not isinstance(node, str) or not node for node in nodes)
        or len(set(nodes)) != len(nodes)
        or any(node in nodes for node in collection_skips)
        or result.get("collected") != len(nodes)
        or result.get("deselected") != 0
    ):
        raise ReproductionError("upstream suite result differs from approved scope or identities")


# Distinguish the outer source-only check from an unavailable pinned API.
_OUTER_CHECK = object()


def _upstream_evidence(
    case_dir: Path,
    case: dict[str, Any],
    report: dict[str, Any],
    registry: Any = _OUTER_CHECK,
) -> dict[str, Any]:
    summary = json.loads((case_dir / "upstream-suite/summary.json").read_bytes())
    if not isinstance(summary, dict):
        raise ReproductionError("invalid upstream suite summary")
    profile = report.get("baseline_profile")
    command, pids = summary.get("command"), report.get("baseline_pids_limit")
    if profile not in _UPSTREAM_PROFILES:
        raise ReproductionError("unsupported upstream suite profile")
    if registry is not _OUTER_CHECK:
        if not callable(registry):
            raise ReproductionError("pinned baseline command registry is unavailable")
        try:
            canonical = list(registry(profile))
        except (ValueError, TypeError) as error:
            raise ReproductionError("pinned registry does not support upstream profile") from error
        if command != canonical:
            raise ReproductionError("upstream command differs from pinned registry")
    if (
        summary.get("version") != "1"
        or summary.get("status") != "passed"
        or summary.get("evidence_kind") != "structured_baseline_repeat"
        or summary.get("independent_instrumentation") is not False
        or summary.get("case_id") != case.get("id")
        or summary.get("profile") != profile
        or summary.get("scope") != _upstream_scope(profile)
        or not isinstance(command, list)
        or command != case["env"].get("test_cmd")
        or command != report.get("baseline_command")
        or type(pids) is not int
        or not 8 <= pids <= 256
        or type(summary.get("pids_limit")) is not int
        or summary["pids_limit"] != pids
        or summary.get("image") != case["env"].get("image")
        or report.get("image") != case["env"].get("image")
        or report.get("case_id") != case.get("id")
        or summary.get("validation_report_sha256")
        != hashlib.sha256((case_dir / "validation.json").read_bytes()).hexdigest()
        or summary.get("source_hashes") != case.get("source_hashes")
        or report.get("source_hashes") != case.get("source_hashes")
        or report.get("status") != "machine_validated"
        or any(summary.get(key) != report.get(key) for key in _IDENTITY_FIELDS)
        or summary.get("environment_python") != case["env"].get("python")
        or summary.get("dependency_lock_sha256") != case["env"].get("dependency_lock_sha256")
        or not isinstance(summary.get("roles"), dict)
        or set(summary["roles"]) != set(_ROLES)
        or not isinstance(report.get("baseline_reports"), dict)
        or set(report["baseline_reports"]) != set(_ROLES)
    ):
        raise ReproductionError("upstream suite summary binding mismatch")
    for role in _ROLES:
        descriptor = summary["roles"][role]
        path = f"upstream-suite/{role}.json"
        raw = (case_dir / path).read_bytes()
        if (
            descriptor.get("path") != path
            or descriptor.get("sha256") != hashlib.sha256(raw).hexdigest()
            or descriptor.get("bytes") != len(raw)
            or descriptor.get("source_sha256") != case["source_hashes"][role]
        ):
            raise ReproductionError("upstream suite role binding mismatch")
        approved = report["baseline_reports"][role]
        for result in (approved, json.loads(raw)):
            _matching_suite(
                result, approved, profile=profile, command=command, pids=pids, identity=report
            )
    return summary


def _upstream_repeat(
    baseline_module: Any,
    built: Any,
    image: str,
    client: Any,
    report: dict[str, Any],
    output: Path,
) -> dict[str, Any]:
    from revertbench.sandbox.worker import SandboxLimits

    profile, pids = report["baseline_profile"], report["baseline_pids_limit"]
    command = list(baseline_module.baseline_command(profile))
    backend = baseline_module.DockerBaselineRunner(
        image, client=client, profile=profile, limits=SandboxLimits(pids=pids)
    )
    results = {}
    (output / "upstream-suite").mkdir()
    for role in _ROLES:
        result = backend(getattr(built, f"{role}_tree")).model_dump(mode="json")
        _matching_suite(
            result,
            report["baseline_reports"][role],
            profile=profile,
            command=command,
            pids=pids,
            identity=report,
        )
        results[role] = result
        (output / f"upstream-suite/{role}.json").write_text(json.dumps(result, indent=2) + "\n")
    return results


def _check_fresh_upstream(fresh: dict[str, Any], approved: dict[str, Any]) -> None:
    profile = approved["baseline_profile"]
    command, pids = approved["baseline_command"], approved["baseline_pids_limit"]
    if fresh.get("baseline_profile") != profile or fresh.get("baseline_command") != command:
        raise ReproductionError("fresh upstream suite selection differs")
    for role in _ROLES:
        _matching_suite(
            fresh["baseline_reports"][role],
            approved["baseline_reports"][role],
            profile=profile,
            command=command,
            pids=pids,
            identity=approved,
        )


def _reproduce_suites(
    version: str,
    baseline_module: Any,
    built: Any,
    image: str,
    client: Any,
    case: dict[str, Any],
    report: dict[str, Any],
    output: Path,
    case_dir: Path,
) -> dict[str, Any]:
    pids = report.get("baseline_pids_limit", 64)
    if version == "1":
        original = json.loads((case_dir / "official-unittest/summary.json").read_bytes())
        suites = {}
        for role in _ROLES:
            suites[role] = _official_suite(
                getattr(built, f"{role}_tree"),
                image,
                tuple(case["env"]["test_cmd"]),
                client,
                baseline_pids_limit=pids,
            )
            if suites[role]["tests_run"] != original["roles"][role]["tests_run"]:
                raise ReproductionError("official suite count differs from approved evidence")
        return {"official_command": case["env"]["test_cmd"], "official_suites": suites}
    original = _upstream_evidence(
        case_dir, case, report, getattr(baseline_module, "baseline_command", None)
    )
    suites = _upstream_repeat(baseline_module, built, image, client, report, output)
    summary = {**original, "image": image, "roles": {}}
    fresh_report_path = output / "validation.json"
    # The caller persisted the normal fresh report before starting this repeat.
    summary["validation_report_sha256"] = hashlib.sha256(fresh_report_path.read_bytes()).hexdigest()
    for role in _ROLES:
        path = f"upstream-suite/{role}.json"
        raw = (output / path).read_bytes()
        summary["roles"][role] = {
            "path": path,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "source_sha256": report["source_hashes"][role],
        }
    (output / "upstream-suite/summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return {
        "suite_evidence_kind": "structured_baseline_repeat",
        "independent_instrumentation": False,
        "upstream_command": original["command"],
        "upstream_scope": original["scope"],
        "upstream_suites": suites,
    }


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
    tree: Path, image: str, command: tuple[str, ...], client: Any, *, baseline_pids_limit: int = 64
) -> dict[str, Any]:
    from revertbench.sandbox.agent import AgentSandbox
    from revertbench.sandbox.worker import SandboxLimits

    if command != ("/usr/local/bin/python", "-m", "unittest", "-q"):
        raise ReproductionError("unsupported version-1 official suite command")
    with AgentSandbox(
        image, tree, client=client, limits=SandboxLimits(pids=baseline_pids_limit)
    ) as box:
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
        "pids_limit": baseline_pids_limit,
        "tests_run": int(counts[0]),
        "exit_code": result.exit_code,
        "stdout": result.stdout.decode("utf-8", errors="replace"),
        "stderr": stderr,
    }


def _baseline_options(validate: Any, report: dict[str, Any]) -> dict[str, Any]:
    """Replay recorded options without requiring additions in legacy pinned APIs."""
    profile = report.get("baseline_profile", "pytest")
    pids = report.get("baseline_pids_limit", 64)
    if type(pids) is not int or not 8 <= pids <= 256:
        raise ReproductionError("recorded baseline PID resource limit is invalid")
    options: dict[str, Any] = {}
    parameters = inspect.signature(validate).parameters
    if "baseline_profile" in parameters:
        options["baseline_profile"] = profile
    elif profile != "pytest":
        raise ReproductionError("pinned grader does not support the recorded baseline profile")
    if "baseline_pids_limit" in parameters:
        options["baseline_pids_limit"] = pids
    elif pids != 64:
        raise ReproductionError(
            "pinned grader does not support the recorded baseline PID resource limit"
        )
    return options


def _inside_pinned_grader(case_dir: Path, output: Path, cache: Path, pinned_root: Path) -> None:
    import docker

    import revertbench
    from revertbench.build import materialize_candidate
    from revertbench.mine.git import GitRepository
    from revertbench.mine.models import candidate_from_json
    from revertbench.review import _machine_passed, build_review_packet, load_case
    from revertbench.sandbox import image as image_module
    from revertbench.validate import baseline as baseline_module
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
    raw_case = json.loads((case_dir / "case.json").read_bytes())
    if publication["manifest_version"] == "2":
        _upstream_evidence(
            case_dir, raw_case, original_report, getattr(baseline_module, "baseline_command", None)
        )
    image_module._BASE_IMAGE = publication["build"]["base_image"]
    # Quiet official suites can emit nothing until completion. The transport
    # timeout must exceed their independent 60-second command wall budget.
    client = docker.from_env(timeout=70)
    try:
        client.ping()
        image = image_module.build_worker_image(client)
        with TemporaryDirectory(prefix="trees-", dir=cache) as temporary:
            built = materialize_candidate(repository, candidate, Path(temporary) / "case")
            case_proof = getattr(case, "repair_derivation", None)
            built_proof = getattr(built, "repair_derivation", None)
            expected_proof = None if case_proof is None else case_proof.content_digest()
            fresh_proof = None if built_proof is None else built_proof.content_digest()
            if (
                built.case_id != case.id
                or built.source_hashes != case.source_hashes
                or built.changed_lines != case.changed_lines
                or built.patch_sha256 != case.repair.patch_sha256
                or hashlib.sha256(built.reference_source.encode()).hexdigest()
                != case.reference_test.source_sha256
                or fresh_proof != expected_proof
            ):
                raise ReproductionError("reconstructed immutable case inputs differ")
            kwargs: dict[str, Any] = {"image": image, "client": client}
            profile = original_report.get("baseline_profile", "pytest")
            kwargs.update(_baseline_options(validate_built_candidate, original_report))
            baseline_pids = original_report.get("baseline_pids_limit", 64)
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
                or getattr(fresh, "baseline_pids_limit", 64) != baseline_pids
                or getattr(fresh, "repair_derivation_sha256", None) != expected_proof
            ):
                raise ReproductionError(
                    "fresh validation did not reproduce approved identities and behavior"
                )
            if publication["manifest_version"] == "2":
                _check_fresh_upstream(fresh.model_dump(mode="json"), original_report)
            suites = _reproduce_suites(
                publication["manifest_version"],
                baseline_module,
                built,
                image,
                client,
                raw_case,
                original_report,
                output,
                case_dir,
            )
            result = {
                "reproduction_version": publication["manifest_version"],
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
                "baseline_pids_limit": baseline_pids,
                "repair_derivation_sha256": expected_proof,
                "reference_score": fresh.reference.score,
                **suites,
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
