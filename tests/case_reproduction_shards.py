"""Deterministic CI planning for complete published case reproductions."""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from pathlib import Path


def parse_shard(value: str) -> tuple[int, int]:
    """Validate one-based indices for the one- or two-runner CI layout."""
    if re.fullmatch(r"[12]/[12]", value) is None:
        raise ValueError(
            "case reproduction shard must be INDEX/TOTAL with 1 <= INDEX <= TOTAL <= 2"
        )
    index, total = (int(part) for part in value.split("/"))
    if index > total:
        raise ValueError(
            "case reproduction shard must be INDEX/TOTAL with 1 <= INDEX <= TOTAL <= 2"
        )
    return index, total


def shard_matrix(case_count: int) -> dict[str, list[dict[str, int]]]:
    """Avoid an empty second runner when only one published case exists."""
    if case_count < 1:
        raise ValueError("at least one published case is required")
    total = min(2, case_count)
    return {"include": [{"index": index, "total": total} for index in range(1, total + 1)]}


def selected_case_ids(case_ids: Collection[str], shard: tuple[int, int]) -> frozenset[str]:
    index, total = parse_shard(f"{shard[0]}/{shard[1]}")
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("duplicate case identities cannot be sharded")
    return frozenset(sorted(case_ids)[index - 1 :: total])


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1] / "cases"
    print(json.dumps(shard_matrix(len(tuple(root.glob("*/case.json"))))))
