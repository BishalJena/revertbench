"""Serializable, unaccepted mining candidates with exact historical provenance."""

from __future__ import annotations

import json
import re
from dataclasses import MISSING, asdict, dataclass, fields
from pathlib import PurePosixPath
from typing import Literal

CommitOutcome = Literal["pass", "fail", "inconclusive"]


def exact_commit(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) not in {40, 64}
        or not re.fullmatch(r"[0-9a-f]+", value)
    ):
        raise ValueError("Commit IDs must be complete lowercase hexadecimal object IDs")
    return value


def relative_path(value: str) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("Repository paths must be bounded strings")
    path = PurePosixPath(value)
    if (
        not value
        or not path.parts
        or path.is_absolute()
        or path.as_posix() != value
        or ".." in path.parts
        or any(character in value for character in "\\\x00\r\n")
    ):
        raise ValueError("Repository paths must be canonical relative POSIX paths")
    return value


@dataclass(frozen=True)
class MiningCandidate:
    repo: str
    fix_commit: str
    fix_parent: str
    reference_path: str
    reference_node_id: str
    reference_source: str
    production_patch: str
    modified_source_paths: tuple[str, ...]
    title: str = ""
    committed_at: str | None = None
    commit_url: str | None = None
    base_commit: str | None = None
    bad_commit: str | None = None
    license_path: str | None = None
    license_text: str | None = None
    reference_header: str | None = None
    reference_derivation: str = "standalone_function_dependency_closure"
    repair_derivation: str = "historical_diff"
    repair_target: str | None = None

    def __post_init__(self) -> None:
        nullable = {
            "committed_at",
            "commit_url",
            "base_commit",
            "bad_commit",
            "license_path",
            "license_text",
            "reference_header",
            "repair_target",
        }
        for field in fields(self):
            value = getattr(self, field.name)
            if value is None and field.name not in nullable:
                raise ValueError("Required candidate fields cannot be null")
            if (
                field.name != "modified_source_paths"
                and value is not None
                and not isinstance(value, str)
            ):
                raise ValueError("Candidate fields must be strings or null")
            if isinstance(value, str):
                try:
                    size = len(value.encode("utf-8"))
                except UnicodeError as error:
                    raise ValueError("Candidate fields must be UTF-8") from error
                limit = {
                    "production_patch": 2 * 1024 * 1024,
                    "reference_source": 65536,
                    "license_text": 1048576,
                    "reference_header": 1048576,
                }.get(field.name, 16384)
                if size > limit:
                    raise ValueError("Candidate field byte limit exceeded")
        if (
            not isinstance(self.modified_source_paths, tuple)
            or len(self.modified_source_paths) > 200
            or any(not isinstance(path, str) for path in self.modified_source_paths)
        ):
            raise ValueError("Modified source paths must be a bounded tuple of strings")
        if any(
            not isinstance(value, str)
            for value in (
                self.repo,
                self.fix_commit,
                self.fix_parent,
                self.reference_path,
                self.reference_node_id,
                self.reference_source,
                self.production_patch,
            )
        ):
            raise ValueError("Required candidate fields must be strings")
        exact_commit(self.fix_commit)
        exact_commit(self.fix_parent)
        for value in (self.fix_commit, self.fix_parent, self.base_commit, self.bad_commit):
            if value is not None:
                exact_commit(value)
        relative_path(self.reference_path)
        if (
            not self.repo
            or not self.reference_node_id
            or not self.reference_source.strip()
            or not self.production_patch
            or not self.modified_source_paths
        ):
            raise ValueError("Candidate identity, reference and production repair must be nonempty")
        for path in self.modified_source_paths:
            relative_path(path)
        if (self.base_commit is None) != (self.bad_commit is None):
            raise ValueError("A verified regression boundary needs both base and bad commits")
        if self.license_path is not None:
            relative_path(self.license_path)
        if self.repair_derivation == "historical_diff":
            if self.repair_target is not None:
                raise ValueError("native historical repairs cannot declare a derived target")
        elif self.repair_derivation == "historical_function_body_v1":
            if (
                self.repair_target is None
                or not self.repair_target.isidentifier()
                or len(self.modified_source_paths) != 1
            ):
                raise ValueError(
                    "function-body repairs require one source path and one function name"
                )
        else:
            raise ValueError("unknown repair derivation")


def candidate_to_json(candidate: MiningCandidate) -> str:
    return json.dumps(asdict(candidate), indent=2, sort_keys=True) + "\n"


def candidate_from_json(source: str) -> MiningCandidate:
    if not isinstance(source, str) or len(source) > 4 * 1024 * 1024:
        raise ValueError("Candidate JSON byte limit exceeded")
    try:
        if len(source.encode("utf-8")) > 4 * 1024 * 1024:
            raise ValueError("Candidate JSON byte limit exceeded")
    except UnicodeError as error:
        raise ValueError("Candidate JSON must be UTF-8") from error
    data = json.loads(source)
    if not isinstance(data, dict) or set(data) - {field.name for field in fields(MiningCandidate)}:
        raise ValueError("Candidate JSON must contain only known object fields")
    required = {
        field.name
        for field in fields(MiningCandidate)
        if field.default is MISSING and field.default_factory is MISSING
    }
    if not required <= data.keys():
        raise ValueError("Candidate JSON is missing required fields")
    paths = data.get("modified_source_paths")
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        raise ValueError("Modified source paths must be an array of strings")
    for key, value in data.items():
        if key != "modified_source_paths" and value is not None and not isinstance(value, str):
            raise ValueError("Candidate fields must be strings or null")
    data["modified_source_paths"] = tuple(paths)
    return MiningCandidate(**data)


@dataclass(frozen=True)
class BisectResult:
    base_commit: str
    bad_commit: str
    observations: tuple[tuple[str, CommitOutcome], ...]
