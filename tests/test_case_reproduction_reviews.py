"""Reproduction launches only for explicit review; the pinned engine proves bindings."""

import hashlib
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _helper(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from scripts import reproduce_case

    monkeypatch.setattr("scripts.reproduce_case.shutil.which", lambda name: "/synthetic/uv")
    return reproduce_case


def _publication(tmp_path: Path, **reviews: Any) -> Path:
    root = tmp_path / "synthetic-case"
    root.mkdir()
    case = {
        "validation": {"status": "accepted"},
        "global_suite_evidence": {"implementation_commit": "a" * 40},
        **reviews,
    }
    raw = json.dumps(case).encode()
    (root / "case.json").write_bytes(raw)
    manifest = {
        "manifest_version": "1",
        "files": {"case.json": {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}},
        "build": {"base_image": "python@sha256:" + "b" * 64},
    }
    (root / "publication-manifest.json").write_text(json.dumps(manifest))
    return root


def _human(**changes: Any) -> dict[str, Any]:
    return {
        "decision": "accept",
        "reviewer": "Synthetic human fixture",
        "attested": True,
        **changes,
    }


def _agent(**changes: Any) -> dict[str, Any]:
    return {
        "reviewer_kind": "agent",
        "decision": "accept",
        "reviewer": "Synthetic agent fixture",
        "authorization_note": "Synthetic owner delegation fixture",
        **changes,
    }


@pytest.mark.parametrize("reviewer_kind", ["legacy-human", "agent"])
def test_explicit_review_reaches_pinned_engine_without_new_schema_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reviewer_kind: str
) -> None:
    helper = _helper(monkeypatch)
    reviews = (
        {"human_review": _human()}
        if reviewer_kind == "legacy-human"
        else {"human_review": None, "agent_review": _agent()}
    )
    root = _publication(tmp_path, **reviews)

    class PinnedEngineReached(RuntimeError):
        pass

    def launch(repo: Path, commit: str, destination: Path) -> None:
        assert commit == "a" * 40
        raise PinnedEngineReached

    monkeypatch.setattr(helper, "_export_grader", launch)
    with pytest.raises(PinnedEngineReached):
        helper.reproduce(root, output=tmp_path / "out", cache=tmp_path / "cache")


@pytest.mark.parametrize(
    "reviews",
    [
        {},
        {"human_review": None, "agent_review": None},
        {"human_review": _human(), "agent_review": _agent()},
        {"human_review": _human(decision="reject")},
        {"human_review": _human(attested=False)},
        {"human_review": _human(attested=1)},
        {"human_review": _human(reviewer=" ")},
        {"agent_review": _agent(decision="reject")},
        {"agent_review": _agent(decision="notes")},
        {"agent_review": _agent(reviewer=" ")},
        {"agent_review": _agent(authorization_note=" ")},
        {"agent_review": _agent(authorization_note=None)},
        {"agent_review": _agent(reviewer_kind="human")},
        {"agent_review": _agent(attested=True)},
        {"agent_review": {"decision": "accept", "reviewer": "Synthetic agent fixture"}},
    ],
)
def test_incomplete_or_conflicting_review_stops_before_any_pinned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reviews: dict[str, Any]
) -> None:
    helper = _helper(monkeypatch)
    root = _publication(tmp_path, **reviews)

    def no_launch(*args: Any, **kwargs: Any) -> None:
        pytest.fail("unreviewed input reached the pinned process")

    monkeypatch.setattr(helper, "_export_grader", no_launch)
    monkeypatch.setattr(helper, "_run", no_launch)
    with pytest.raises(helper.ReproductionError, match="explicitly accepted"):
        helper.reproduce(root, output=tmp_path / "out", cache=tmp_path / "cache")
    assert not (tmp_path / "out").exists()
