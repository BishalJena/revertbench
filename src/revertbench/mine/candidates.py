"""Find small source-only repair candidates; discovery never accepts a case."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from revertbench.grade.policy import check_submission
from revertbench.mine.git import GitError, GitRepository
from revertbench.mine.models import MiningCandidate
from revertbench.mine.reference import (
    ReferenceError,
    changed_test_functions,
    extract_reference,
    reference_derivation,
    reference_header,
)

_FIX_PATTERN = re.compile(r"\b(regressions?|fix(?:es|ed|ing)?|introduced|revert(?:ed)?)\b", re.I)
_EXCLUDED_SOURCE = {
    "test",
    "tests",
    "testing",
    "docs",
    "examples",
    "benchmarks",
    "scripts",
    "tools",
    ".github",
}


@dataclass(frozen=True)
class DiscoveryReport:
    candidates: tuple[MiningCandidate, ...]
    examined_commits: int
    matched_fixes: int
    rejections: tuple[tuple[str, str], ...]


def production_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        path
        for path in paths
        if path.endswith(".py")
        and not _EXCLUDED_SOURCE.intersection(PurePosixPath(path).parts)
        and PurePosixPath(path).name not in {"setup.py", "conftest.py", "noxfile.py"}
        and not _test_path(path)
    )


def _test_path(path: str) -> bool:
    selected = PurePosixPath(path)
    return selected.suffix == ".py" and (
        path == "test.py" or "tests" in selected.parts or selected.name.startswith("test_")
    )


def _license(repository: GitRepository, commit: str) -> tuple[str | None, str | None]:
    for path in ("LICENSE", "LICENSE.txt", "LICENSE.rst", "LICENSE.md", "COPYING"):
        if repository.file_exists(commit, path):
            return path, repository.file_text(commit, path)
    return None, None


def _before(repository: GitRepository, commit: str, path: str) -> str:
    return repository.file_text(commit, path) if repository.file_exists(commit, path) else ""


def _repair(repository: GitRepository, parent: str, fix: str, paths: tuple[str, ...]) -> str:
    patch = repository.diff(parent, fix, paths=paths)
    changed = sum(
        line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        for line in patch.splitlines()
    )
    if not patch or changed >= 200 or len(patch.encode("utf-8")) > 2 * 1024 * 1024:
        raise ReferenceError("production_patch_size_filter")
    if re.search(
        r"(?m)^(?:new file mode|old mode|new mode|deleted file mode) (?:120000|160000)$", patch
    ):
        raise ReferenceError("production_patch_unsupported_mode")
    return patch


def _fix_candidates(repository: GitRepository, fix: str) -> tuple[list[MiningCandidate], list[str]]:
    parent = repository.parent(fix)
    paths = repository.changed_paths(parent, fix)
    source_paths = production_paths(paths)
    test_paths = tuple(path for path in paths if _test_path(path))
    if not source_paths or not test_paths:
        raise ReferenceError("no_production_and_changed_test_pair")
    patch = _repair(repository, parent, fix, source_paths)
    title, committed_at, _ = repository.commit_metadata(fix)
    license_path, license_text = _license(repository, fix)
    expected_url = f"https://github.com/{repository.repo}"
    observed_url = repository.remote_url()
    commit_url = (
        f"{expected_url}/commit/{fix}"
        if observed_url
        in {
            expected_url,
            f"{expected_url}.git",
        }
        else None
    )
    references: list[tuple[str, str, str, str, str]] = []
    rejected: list[str] = []
    for path in test_paths:
        try:
            after = repository.file_text(fix, path)
            before = _before(repository, parent, path)
            for function in changed_test_functions(before, after):
                try:
                    references.append(
                        (
                            path,
                            function,
                            extract_reference(after, function),
                            reference_header(after),
                            reference_derivation(after, function),
                        )
                    )
                except ReferenceError as error:
                    rejected.append(str(error))
        except (ReferenceError, GitError, UnicodeError) as error:
            rejected.append(str(error))
    if not references:
        raise ReferenceError("no_supported_changed_top_level_reference")
    candidates: list[MiningCandidate] = []
    repository.work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="policy-", dir=repository.work_root) as temporary:
        bad_tree = repository.export_tree(parent, Path(temporary) / "bad")
        for path, function, source, header, derivation in references:
            try:
                policy = check_submission(source, bad_tree)
                if not policy.allowed:
                    raise ReferenceError(f"unsupported_reference_policy:{policy.reason}")
                candidates.append(
                    MiningCandidate(
                        repo=repository.repo,
                        fix_commit=fix,
                        fix_parent=parent,
                        reference_path=path,
                        reference_node_id=f"{path}::{function}",
                        reference_source=source,
                        production_patch=patch,
                        modified_source_paths=source_paths,
                        title=title,
                        committed_at=committed_at,
                        commit_url=commit_url,
                        license_path=license_path,
                        license_text=license_text,
                        reference_header=header,
                        reference_derivation=derivation,
                    )
                )
                break  # Initial yield counts distinct fixes, not reference variants.
            except ReferenceError as error:
                rejected.append(str(error))
    return candidates, rejected


def discover_report(
    repository: GitRepository,
    *,
    limit: int = 20,
    max_commits: int = 1000,
) -> DiscoveryReport:
    if not 1 <= limit <= 1000:
        raise ValueError("Candidate limit must be between 1 and 1000")
    candidates: list[MiningCandidate] = []
    rejected: list[tuple[str, str]] = []
    matched, examined = 0, 0
    for commit in repository.first_parent_history(limit=max_commits):
        examined += 1
        try:
            _, _, message = repository.commit_metadata(commit)
            if not _FIX_PATTERN.search(message):
                continue
            matched += 1
            selected, reasons = _fix_candidates(repository, commit)
            candidates.extend(selected)
            rejected.extend((commit, reason) for reason in reasons)
        except (GitError, ReferenceError, UnicodeError) as error:
            rejected.append((commit, str(error)))
        if len(candidates) >= limit:
            break
    return DiscoveryReport(tuple(candidates), examined, matched, tuple(rejected))


def discover_candidates(
    repository: GitRepository,
    *,
    limit: int = 20,
    max_commits: int = 1000,
) -> list[MiningCandidate]:
    return list(discover_report(repository, limit=limit, max_commits=max_commits).candidates)
