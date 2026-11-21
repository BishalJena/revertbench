from pathlib import Path

import pytest

from revertbench.models.ledger import JsonlLedger, LedgerError, request_identity


def test_completed_response_replays_only_same_logical_identity_and_request(tmp_path: Path) -> None:
    with JsonlLedger(tmp_path / "calls.jsonl") as ledger:
        ledger.append("model_started", call_id="sample/turn/attempt", request_hash="abc")
        assert ledger.model_response("sample/turn/attempt", "abc") is None
        ledger.append(
            "model_completed",
            call_id="sample/turn/attempt",
            request_hash="abc",
            response={"text": "done"},
        )
        assert ledger.model_response("sample/turn/attempt", "abc") == {"text": "done"}
        assert ledger.model_response("independent/turn/attempt", "abc") is None
        with pytest.raises(LedgerError, match="changed"):
            ledger.model_response("sample/turn/attempt", "different")
    with JsonlLedger(tmp_path / "calls.jsonl") as resumed:
        assert resumed.model_response("sample/turn/attempt", "abc") == {"text": "done"}


def test_uncertain_started_call_remains_distinct_from_missing_or_completed(tmp_path: Path) -> None:
    with JsonlLedger(tmp_path / "calls.jsonl") as ledger:
        ledger.append("model_started", call_id="call", request_hash="hash")
        assert ledger.started("call")
        assert not ledger.started("missing")
        assert ledger.model_response("call", "hash") is None


def test_ledger_refuses_second_writer_and_incomplete_tail(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    with JsonlLedger(path), pytest.raises(LedgerError, match="writer"), JsonlLedger(path):
        pass
    path.write_text('{"event":')
    with pytest.raises(LedgerError, match="incomplete"), JsonlLedger(path):
        pass


def test_full_request_identity_includes_provider_revision_and_exact_wire_bytes() -> None:
    assert request_identity(b"{}", {"provider": "a", "revision": None}) != request_identity(
        b"{}", {"provider": "b", "revision": None}
    )
    assert request_identity(b"{}", {"revision": "1"}) != request_identity(b"{}", {"revision": "2"})
    assert request_identity(b"{}", {}) != request_identity(b"{ }", {})


def test_same_ledger_object_can_reenter_without_duplicating_events(tmp_path: Path) -> None:
    ledger = JsonlLedger(tmp_path / "calls.jsonl")
    with ledger:
        ledger.append("one")
    with ledger:
        assert len(ledger.events) == 1
        assert ledger.append("two")["seq"] == 1


def test_conflicting_completed_responses_cannot_replace_a_cached_result(tmp_path: Path) -> None:
    with JsonlLedger(tmp_path / "calls.jsonl") as ledger:
        ledger.append("model_completed", call_id="call", request_hash="hash", response={"value": 1})
        with pytest.raises(LedgerError, match="conflicting"):
            ledger.append(
                "model_completed", call_id="call", request_hash="hash", response={"value": 2}
            )


def test_first_intent_and_checkpoint_fsync_parent_before_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import stat

    from revertbench.models.ledger import atomic_json

    observed: list[str] = []
    original = os.fsync

    def spy(descriptor: int) -> None:
        observed.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        original(descriptor)

    monkeypatch.setattr(os, "fsync", spy)
    with JsonlLedger(tmp_path / "calls.jsonl") as ledger:
        ledger.append("intent")
        observed.append("external_send")
    assert observed == ["file", "directory", "external_send"]
    observed.clear()
    atomic_json(tmp_path / "manifest.json", {"id": "experiment"})
    assert observed == ["file", "directory"]
