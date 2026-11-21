"""Conservative unified-diff admission for controlled production repairs."""

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

_HEADER = re.compile(r"^diff --git a/([^\s]+) b/([^\s]+)$")
_HUNK = re.compile(r"^@@ -(\d{1,8})(?:,(\d{1,8}))? \+(\d{1,8})(?:,(\d{1,8}))? @@(?:.*)$")
_INDEX = re.compile(r"^index [0-9a-f]+\.\.[0-9a-f]+(?: 100644)?$")
_FORBIDDEN = {
    "tests",
    "test",
    "testing",
    "docs",
    "examples",
    "scripts",
    "configs",
    "context",
    "benchmarks",
}


class BuildError(ValueError):
    """A candidate cannot be built within the supported controlled-pair contract."""


def production_path(name: str) -> str:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or str(path) != name
        or ".." in path.parts
        or "\\" in name
        or path.suffix != ".py"
        or any(part.startswith(".") or part in _FORBIDDEN for part in path.parts)
        or path.name.startswith("test_")
        or path.name in {"conftest.py", "setup.py"}
        or any(ord(character) < 33 or ord(character) > 126 for character in name)
    ):
        raise BuildError("unsupported_production_path")
    return name


@dataclass(frozen=True)
class ParsedPatch:
    paths: tuple[str, ...]
    added_lines: dict[str, tuple[int, ...]]
    removed_lines: dict[str, tuple[int, ...]]
    changed_count: int


@dataclass
class _Parser:
    paths: list[str] = field(default_factory=list)
    added: dict[str, list[int]] = field(default_factory=dict)
    removed: dict[str, list[int]] = field(default_factory=dict)
    path: str | None = None
    old_header: bool = False
    new_header: bool = False
    hunk_seen: bool = False
    old_left: int = 0
    new_left: int = 0
    old_line: int = 0
    new_line: int = 0

    def line(self, line: str) -> None:
        if self.old_left or self.new_left:
            self._content(line)
            return
        if line == "\\ No newline at end of file":
            return
        if header := _HEADER.fullmatch(line):
            if self.path is not None and not self.hunk_seen:
                raise BuildError("missing_patch_hunk")
            before, after = header.groups()
            if before != after:
                raise BuildError("renamed_production_path")
            self.path = production_path(after)
            if self.path in self.paths:
                raise BuildError("duplicate_patch_path")
            self.paths.append(self.path)
            self.old_header = self.new_header = self.hunk_seen = False
            return
        if self.path is None:
            raise BuildError("invalid_patch_header")
        if _INDEX.fullmatch(line) or line in {"new file mode 100644", "deleted file mode 100644"}:
            return
        if line.startswith("--- "):
            if self.old_header or line[4:] not in {f"a/{self.path}", "/dev/null"}:
                raise BuildError("invalid_old_patch_path")
            self.old_header = True
            return
        if line.startswith("+++ "):
            if self.new_header or line[4:] not in {f"b/{self.path}", "/dev/null"}:
                raise BuildError("invalid_new_patch_path")
            self.new_header = True
            return
        if hunk := _HUNK.fullmatch(line):
            if not self.old_header or not self.new_header:
                raise BuildError("missing_patch_paths")
            self.old_line, old_count, self.new_line, new_count = (
                int(value) if value is not None else 1 for value in hunk.groups()
            )
            self.old_left, self.new_left = old_count, new_count
            if self.old_left == self.new_left == 0:
                raise BuildError("empty_patch_hunk")
            self.hunk_seen = True
            return
        raise BuildError("unsupported_patch_metadata")

    def _content(self, line: str) -> None:
        if line == "\\ No newline at end of file":
            return
        if not line or line[0] not in " +-" or self.path is None:
            raise BuildError("invalid_patch_hunk")
        kind = line[0]
        if kind in " -":
            if self.old_left < 1:
                raise BuildError("inconsistent_patch_hunk")
            if kind == "-":
                self.removed.setdefault(self.path, []).append(self.old_line)
            self.old_left -= 1
            self.old_line += 1
        if kind in " +":
            if self.new_left < 1:
                raise BuildError("inconsistent_patch_hunk")
            if kind == "+":
                self.added.setdefault(self.path, []).append(self.new_line)
            self.new_left -= 1
            self.new_line += 1


def parse_production_patch(patch: str, *, max_changed_lines: int = 400) -> ParsedPatch:
    """Validate paths, regular-file modes and hunk counts without running Git or source."""
    if not patch or len(patch.encode("utf-8")) > 100_000:
        raise BuildError("unsupported_patch_size")
    parser = _Parser()
    lines = patch.split("\n")
    if lines[-1] == "":
        lines.pop()
    for line in lines:
        parser.line(line)
    if not parser.paths or not parser.hunk_seen or parser.old_left or parser.new_left:
        raise BuildError("incomplete_production_patch")
    changed = sum(map(len, parser.added.values())) + sum(map(len, parser.removed.values()))
    if changed == 0 or changed > max_changed_lines:
        raise BuildError("unsupported_patch_size")
    return ParsedPatch(
        tuple(parser.paths),
        {path: tuple(values) for path, values in parser.added.items()},
        {path: tuple(values) for path, values in parser.removed.items()},
        changed,
    )
