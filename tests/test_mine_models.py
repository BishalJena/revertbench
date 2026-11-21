from dataclasses import replace

import pytest

from revertbench.mine.models import MiningCandidate, candidate_from_json, candidate_to_json


def test_candidate_round_trip_preserves_pinned_provenance_and_optional_boundary() -> None:
    candidate = MiningCandidate(
        repo="owner/repo",
        fix_commit="a" * 40,
        fix_parent="b" * 40,
        reference_path="tests/test_api.py",
        reference_node_id="test_regression",
        reference_source="def test_regression():\n    assert True\n",
        production_patch="diff --git a/pkg.py b/pkg.py\n",
        modified_source_paths=("pkg.py",),
    )
    assert candidate_from_json(candidate_to_json(candidate)) == candidate
    assert replace(candidate, base_commit="c" * 40, bad_commit="d" * 40).bad_commit == "d" * 40
    assert candidate.committed_at is None


@pytest.mark.parametrize("sha", ["main", "--option", "a" * 39, "z" * 40])
def test_candidate_requires_exact_commit_ids(sha: str) -> None:
    with pytest.raises(ValueError):
        MiningCandidate(
            repo="owner/repo",
            fix_commit=sha,
            fix_parent="b" * 40,
            reference_path="tests/test_api.py",
            reference_node_id="test_regression",
            reference_source="def test_regression():\n    assert True\n",
            production_patch="patch",
            modified_source_paths=("pkg.py",),
        )


def test_candidate_json_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError):
        candidate_from_json('{"unexpected": true}')


@pytest.mark.parametrize("field", ["repo", "fix_commit", "reference_source", "production_patch"])
def test_required_candidate_fields_cannot_be_null(field: str) -> None:
    import json

    candidate = MiningCandidate(
        repo="owner/repo",
        fix_commit="a" * 40,
        fix_parent="b" * 40,
        reference_path="tests/test_api.py",
        reference_node_id="test_regression",
        reference_source="def test_regression():\n    assert True\n",
        production_patch="patch",
        modified_source_paths=("pkg.py",),
    )
    data = json.loads(candidate_to_json(candidate))
    data[field] = None
    with pytest.raises(ValueError):
        candidate_from_json(json.dumps(data))


@pytest.mark.parametrize("field", ["title", "reference_derivation", "modified_source_paths"])
def test_direct_candidate_rejects_null_nonnullable_fields(field: str) -> None:
    import json

    data = {
        "repo": "owner/repo",
        "fix_commit": "a" * 40,
        "fix_parent": "b" * 40,
        "reference_path": "tests/test_api.py",
        "reference_node_id": "test_regression",
        "reference_source": "def test_regression():\n    assert True\n",
        "production_patch": "patch",
        "modified_source_paths": ["pkg.py"],
    }
    data[field] = None  # type: ignore[assignment]
    with pytest.raises(ValueError):
        candidate_from_json(json.dumps(data))


def test_candidate_json_has_a_preparse_byte_bound() -> None:
    with pytest.raises(ValueError, match="limit"):
        candidate_from_json(" " * (4 * 1024 * 1024 + 1))
