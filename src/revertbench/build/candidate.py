"""Pristine source snapshots and controlled repairs; repository code is never executed here."""

import hashlib
import io
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from revertbench.build.derivation import DerivedFunctionRepair, derive_function_body_patch
from revertbench.build.patch import BuildError, parse_production_patch, production_path
from revertbench.grade.coverage_check import normalize_changed_lines
from revertbench.mine.models import MiningCandidate, exact_commit
from revertbench.mine.reference import (
    ReferenceError,
    changed_test_functions,
    extract_reference,
    reference_derivation,
)
from revertbench.sandbox.archives import ArchiveError, archive_tree
from revertbench.schema import RepairDerivation

_MAX_INTRODUCING_CHANGED_LINES = 700


class RepositorySnapshotter(Protocol):
    def resolve_commit(self, commit: str) -> str: ...
    def parent(self, commit: str) -> str: ...
    def is_ancestor(self, ancestor: str, descendant: str) -> bool: ...
    def file_text(self, commit: str, path: str) -> str: ...
    def file_exists(self, commit: str, path: str) -> bool: ...
    def export_tree(self, commit: str, destination: Path) -> Path: ...
    def changed_paths(self, before: str, after: str) -> tuple[str, ...]: ...
    def diff(self, before: str, after: str, *, paths: tuple[str, ...] = ()) -> str: ...
    def apply_patch(self, tree: Path, patch: str, *, check: bool = False) -> None: ...


@dataclass(frozen=True)
class BuiltCandidate:
    case_id: str
    repo: str
    base_commit: str
    bad_commit: str
    fix_commit: str
    fix_parent: str
    base_tree: Path
    bad_tree: Path
    historical_fix_tree: Path
    repaired_tree: Path
    reference_source: str
    reference_path: str
    reference_node_id: str
    production_patch: str
    patch_sha256: str
    source_hashes: dict[str, str]
    changed_lines: dict[str, tuple[int, ...]]
    candidate: MiningCandidate
    repair_derivation: RepairDerivation | None = None
    historical_production_diff: str | None = None


def canonical_tree_digest(tree: Path) -> str:
    return hashlib.sha256(_source_archive(tree)).hexdigest()


def _source_archive(tree: Path) -> bytes:
    try:
        return archive_tree(tree, writable=False)
    except (ArchiveError, OSError) as error:
        raise BuildError("source_archive_invalid") from error


def _file_hashes(payload: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        for member in archive:
            if member.isfile():
                stream = archive.extractfile(member)
                assert stream is not None
                result[member.name] = hashlib.sha256(stream.read()).hexdigest()
    return result


def _verified_pins(
    repository: RepositorySnapshotter, candidate: MiningCandidate
) -> tuple[str, str]:
    if candidate.base_commit is None or candidate.bad_commit is None:
        raise BuildError("candidate_needs_bisect")
    for commit in (
        candidate.base_commit,
        candidate.bad_commit,
        candidate.fix_commit,
        candidate.fix_parent,
    ):
        if repository.resolve_commit(exact_commit(commit)) != commit:
            raise BuildError("commit_identity_mismatch")
    if repository.parent(candidate.bad_commit) != candidate.base_commit:
        raise BuildError("base_is_not_introducing_parent")
    if repository.parent(candidate.fix_commit) != candidate.fix_parent:
        raise BuildError("historical_fix_parent_mismatch")
    if not repository.is_ancestor(candidate.bad_commit, candidate.fix_parent):
        raise BuildError("bad_is_not_ancestor_of_fix")
    return candidate.base_commit, candidate.bad_commit


def _introduced_lines(
    repository: RepositorySnapshotter, base: str, bad: str, tree: Path
) -> dict[str, tuple[int, ...]]:
    paths: list[str] = []
    for path in repository.changed_paths(base, bad):
        try:
            paths.append(production_path(path))
        except BuildError:
            continue
    if not paths:
        raise BuildError("no_changed_production_lines")
    introduced = parse_production_patch(
        repository.diff(base, bad, paths=tuple(paths)),
        max_changed_lines=_MAX_INTRODUCING_CHANGED_LINES,
    )
    try:
        normalized = normalize_changed_lines(tree, introduced.added_lines)
    except (OSError, UnicodeError, SyntaxError, ValueError) as error:
        raise BuildError("unsupported_bad_source_or_changed_lines") from error
    return {path: tuple(sorted(lines)) for path, lines in normalized.items()}


def materialize_candidate(
    repository: RepositorySnapshotter, candidate: MiningCandidate, destination: Path
) -> BuiltCandidate:
    """Build pinned snapshots and a production-only historical repair."""
    destination = destination.absolute()
    base, bad = _verified_pins(repository, candidate)
    parsed = parse_production_patch(candidate.production_patch, max_changed_lines=200)
    if set(parsed.paths) != set(candidate.modified_source_paths):
        raise BuildError("repair_path_manifest_mismatch")
    historical_patch = repository.diff(
        candidate.fix_parent, candidate.fix_commit, paths=parsed.paths
    )
    derived: DerivedFunctionRepair | None = None
    if candidate.repair_derivation == "historical_diff":
        if historical_patch != candidate.production_patch:
            raise BuildError("repair_is_not_the_historical_production_diff")
    else:
        if len(parsed.paths) != 1 or candidate.repair_target is None:
            raise BuildError("ambiguous_historical_repair_derivation")
        path = parsed.paths[0]
        for changed_path in repository.changed_paths(candidate.fix_parent, candidate.fix_commit):
            if Path(changed_path).name in {
                "pyproject.toml",
                "setup.py",
                "setup.cfg",
                "requirements.txt",
                "Pipfile",
                "Pipfile.lock",
                "poetry.lock",
                "uv.lock",
            }:
                raise BuildError("historical_dependency_change")
            try:
                changed_production = production_path(changed_path)
            except BuildError:
                continue
            if changed_production != path:
                raise BuildError("historical_changes_outside_repair_target")
        if not all(
            repository.file_exists(commit, path)
            for commit in (candidate.fix_parent, candidate.fix_commit, bad)
        ):
            raise BuildError("missing_historical_repair_source")
        derived = derive_function_body_patch(
            repository.file_text(candidate.fix_parent, path),
            repository.file_text(candidate.fix_commit, path),
            repository.file_text(bad, path),
            path=path,
            function_name=candidate.repair_target,
            historical_patch=historical_patch,
        )
        if derived.patch != candidate.production_patch:
            raise BuildError("candidate_patch_is_not_the_derived_historical_repair")
    node_parts = candidate.reference_node_id.split("::")
    if (
        len(node_parts) not in (2, 3)
        or node_parts[0] != candidate.reference_path
        or not all(part.isidentifier() for part in node_parts[1:])
    ):
        raise BuildError("reference_node_mismatch")
    function = "::".join(node_parts[1:])
    try:
        previous = (
            repository.file_text(candidate.fix_parent, candidate.reference_path)
            if repository.file_exists(candidate.fix_parent, candidate.reference_path)
            else ""
        )
        current = repository.file_text(candidate.fix_commit, candidate.reference_path)
        if candidate.reference_derivation != reference_derivation(current, function):
            raise BuildError("reference_derivation_mismatch")
        if function not in changed_test_functions(previous, current):
            raise BuildError("reference_not_changed_in_historical_fix")
        if extract_reference(current, function) != candidate.reference_source:
            raise BuildError("reference_is_not_historical_extraction")
    except ReferenceError as error:
        raise BuildError("historical_reference_unsupported") from error
    destination.mkdir(parents=True, exist_ok=False)
    trees: dict[str, Path] = {}
    archives: dict[str, bytes] = {}
    for role, commit in (("base", base), ("bad", bad), ("historical_fix", candidate.fix_commit)):
        tree = destination / role
        repository.export_tree(commit, tree)
        trees[role] = tree
        archives[role] = _source_archive(tree)
    repaired = destination / "repaired"
    repository.export_tree(bad, repaired)
    if _source_archive(repaired) != archives["bad"]:
        raise BuildError("fresh_bad_export_mismatch")
    repository.apply_patch(repaired, candidate.production_patch, check=True)
    repository.apply_patch(repaired, candidate.production_patch)
    archives["repaired"] = _source_archive(repaired)
    if derived is not None and (
        repaired / derived.proof.path
    ).read_bytes() != derived.repaired_source.encode("utf-8"):
        raise BuildError("unexpected_derived_repair_bytes")
    before, after = _file_hashes(archives["bad"]), _file_hashes(archives["repaired"])
    changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    if changed != set(parsed.paths):
        raise BuildError("unexpected_repair_change")
    changed_lines = _introduced_lines(repository, base, bad, trees["bad"])
    repo_name = re.sub(r"[^A-Za-z0-9_-]", "-", candidate.repo.split("/")[-1])
    node_hash = hashlib.sha256(candidate.reference_node_id.encode()).hexdigest()[:8]
    case_id = f"{repo_name}-{bad[:12]}-{candidate.fix_commit[:8]}-{node_hash}"
    return BuiltCandidate(
        case_id,
        candidate.repo,
        base,
        bad,
        candidate.fix_commit,
        candidate.fix_parent,
        trees["base"],
        trees["bad"],
        trees["historical_fix"],
        repaired,
        candidate.reference_source,
        candidate.reference_path,
        candidate.reference_node_id,
        candidate.production_patch,
        hashlib.sha256(candidate.production_patch.encode("utf-8")).hexdigest(),
        {role: hashlib.sha256(payload).hexdigest() for role, payload in archives.items()},
        changed_lines,
        candidate,
        None if derived is None else derived.proof,
        None if derived is None else historical_patch,
    )
