"""Validate changed hunks and attribute executable-line hits to individual tests."""

from collections.abc import Collection, Mapping
from pathlib import Path, PurePosixPath
from types import CodeType

from revertbench.schema import TestResult


def relative_source_path(value: str) -> PurePosixPath:
    """Accept only canonical, project-relative Python source paths."""
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or ".." in path.parts
        or str(path) != value
        or path.suffix != ".py"
    ):
        raise ValueError("changed paths must be canonical relative Python source paths")
    return path


def _executable_lines(code: CodeType) -> set[int]:
    lines = {line for _, _, line in code.co_lines() if line is not None and line > 0}
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            lines.update(_executable_lines(constant))
    return lines


def _source_lines(tree: Path, path: PurePosixPath) -> tuple[str, set[int]]:
    source_path = tree.joinpath(*path.parts)
    current = tree
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("changed source paths cannot contain symlinks")
    if not source_path.is_file() or not source_path.resolve().is_relative_to(tree.resolve()):
        raise ValueError("changed source file is missing or outside the case tree")
    if source_path.stat().st_size > 1_048_576:
        raise ValueError("changed source file exceeds the supported size")
    source = source_path.read_text(encoding="utf-8")
    return source, _executable_lines(compile(source, str(path), "exec"))


def normalize_changed_lines(
    tree: Path, changed_lines: Mapping[str, Collection[int]]
) -> dict[str, frozenset[int]]:
    """Check hunks against bad source and retain their executable lines only.

    Compiling discovers line metadata; no submission or project code is executed.
    Comments and blank lines may be part of a hunk, but cannot earn coverage.
    """
    normalized: dict[str, frozenset[int]] = {}
    if not changed_lines:
        raise ValueError("at least one executable changed line is required")
    for name, lines in changed_lines.items():
        path = relative_source_path(name)
        source, executable = _source_lines(tree, path)
        if not lines or any(type(line) is not int or line < 1 for line in lines):
            raise ValueError("changed line numbers must be positive integers")
        if any(line > len(source.splitlines()) for line in lines):
            raise ValueError("changed line number exceeds its source file")
        hits = frozenset(lines).intersection(executable)
        if hits:
            normalized[name] = hits
    if not normalized:
        raise ValueError("changed hunks contain no executable lines")
    return normalized


def changed_line_hits(
    test: TestResult, changed_lines: Mapping[str, Collection[int]]
) -> dict[str, tuple[int, ...]]:
    """Return changed-line hits from this test's call-phase evidence alone."""
    return {
        path: tuple(sorted(hits))
        for path, lines in changed_lines.items()
        if (hits := set(test.covered_lines.get(path, ())).intersection(lines))
    }
