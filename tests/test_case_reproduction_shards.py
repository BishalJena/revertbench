"""CI partitions complete case reproductions without changing local defaults."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from case_reproduction_shards import parse_shard, selected_case_ids, shard_matrix

_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("count", [1, 6, 10])
def test_matrix_partitions_every_case_once_in_balanced_nonempty_shards(count: int) -> None:
    cases = tuple(f"case-{index:02}" for index in range(count))
    matrix = shard_matrix(count)
    groups = [
        selected_case_ids(cases, (entry["index"], entry["total"])) for entry in matrix["include"]
    ]
    assert len(groups) == min(2, count)
    assert all(groups)
    assert set().union(*groups) == set(cases)
    assert sum(map(len, groups)) == count
    assert max(map(len, groups)) - min(map(len, groups)) <= 1
    assert max(map(len, groups)) <= 5


def test_assignment_uses_sorted_case_identity_not_collection_order() -> None:
    assert selected_case_ids(("c", "a", "b"), (1, 2)) == frozenset({"a", "c"})
    assert selected_case_ids(("b", "c", "a"), (2, 2)) == frozenset({"b"})


@pytest.mark.parametrize("value", ["", "1", "a/2", "0/2", "3/2", "2/1", "1/0", "1/3", "1/2/3"])
def test_invalid_shard_options_fail_closed(value: str) -> None:
    with pytest.raises(ValueError, match="INDEX/TOTAL"):
        parse_shard(value)


@pytest.mark.parametrize("count", [0, -1])
def test_matrix_requires_at_least_one_published_case(count: int) -> None:
    with pytest.raises(ValueError, match="published case"):
        shard_matrix(count)


def test_duplicate_case_identities_are_not_silently_removed() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        selected_case_ids(("case-a", "case-a"), (1, 2))


def _collect(*extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_public_cases.py",
            "--collect-only",
            "-o",
            "addopts=",
            "-q",
            "-k",
            "test_accepted_case_reproduces_with_its_pinned_grader",
            *extra,
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _nodeids(result: subprocess.CompletedProcess[str]) -> set[str]:
    assert result.returncode == 0, result.stdout + result.stderr
    return {line for line in result.stdout.splitlines() if line.startswith("tests/")}


def test_real_collection_default_keeps_all_cases_and_shards_are_disjoint() -> None:
    default = _nodeids(_collect())
    expected = {path.parent.name for path in (_ROOT / "cases").glob("*/case.json")}
    assert len(default) == len(expected)
    assert {node.split("[")[1].removesuffix("]") for node in default} == expected
    groups = [
        _nodeids(_collect(f"--case-reproduction-shard={entry['index']}/{entry['total']}"))
        for entry in shard_matrix(len(expected))["include"]
    ]
    assert all(groups)
    assert set().union(*groups) == default
    assert sum(map(len, groups)) == len(default)
    assert max(map(len, groups)) - min(map(len, groups)) <= 1


def test_invalid_cli_shard_is_a_usage_error_before_test_collection() -> None:
    result = _collect("--case-reproduction-shard=3/2")
    assert result.returncode == 4
    assert "INDEX/TOTAL" in result.stderr


def test_planner_cli_emits_the_same_matrix_used_by_selection() -> None:
    result = subprocess.run(
        [sys.executable, "tests/case_reproduction_shards.py"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    count = len(tuple((_ROOT / "cases").glob("*/case.json")))
    assert json.loads(result.stdout) == shard_matrix(count)


def test_workflow_docker_jobs_cover_every_docker_node_exactly_once() -> None:
    import re
    import shlex

    workflow = (_ROOT / ".github/workflows/ci.yml").read_text()

    def job_command(job: str) -> list[str]:
        block = re.search(rf"^  {job}:\n(.*?)(?=^  [a-z-]+:|\Z)", workflow, re.M | re.S)
        assert block is not None, f"missing required {job} job"
        command = re.search(r"^      - run: (uv run pytest .*)$", block[1], re.M)
        assert command is not None
        return shlex.split(command[1])[3:]

    def collect(arguments: list[str]) -> set[str]:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-o",
                "addopts=",
                "-q",
                *(value for value in arguments if value != "--run-docker"),
            ],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return _nodeids(result)

    complete = collect(["--run-docker", "-m", "docker"])
    boundary = collect(job_command("sandbox-boundaries"))
    reproduction = job_command("case-reproductions")
    assert "--case-reproduction-shard=$CASE_REPRODUCTION_SHARD" in reproduction
    reproduction.remove("--case-reproduction-shard=$CASE_REPRODUCTION_SHARD")
    groups = [collect([*reproduction, f"--case-reproduction-shard={index}/2"]) for index in (1, 2)]
    assert boundary
    assert all(groups)
    assert boundary | groups[0] | groups[1] == complete
    assert not boundary & groups[0]
    assert not boundary & groups[1]
    assert not groups[0] & groups[1]


def test_aggregate_requires_every_job_and_preserves_artifacts() -> None:
    workflow = (_ROOT / ".github/workflows/ci.yml").read_text()
    aggregate = workflow.split("\n  sandbox:\n", 1)[1]
    assert "if: ${{ always() }}" in aggregate
    assert "needs: [reproduction-plan, sandbox-boundaries, case-reproductions]" in aggregate
    for job in ("reproduction-plan", "sandbox-boundaries", "case-reproductions"):
        assert f"needs.{job}.result" in aggregate
    assert 'test "$PLAN_RESULT" = success' in aggregate
    assert 'test "$BOUNDARY_RESULT" = success' in aggregate
    assert 'test "$REPRODUCTION_RESULT" = success' in aggregate
    assert "uses: actions/upload-artifact/merge@v4" in aggregate
    assert "name: accepted-case-reproductions\n" in aggregate
    assert "pattern: accepted-case-reproductions-shard-*" in aggregate
    assert "separate-directories: false" in aggregate
    assert "delete-merged: false" in aggregate
    assert "fail-fast: false" in workflow
