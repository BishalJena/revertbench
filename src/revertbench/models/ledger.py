"""Single-writer durable evidence; completed calls are scoped to logical identity."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Any

_MAX_EVENT = 4 * 1024 * 1024
_MAX_LEDGER = 256 * 1024 * 1024


class LedgerError(RuntimeError):
    pass


def request_identity(body: bytes, context: dict[str, Any]) -> str:
    metadata = json.dumps(context, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(metadata + b"\0" + body).hexdigest()


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".checkpoint-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


class JsonlLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.events: list[dict[str, Any]] = []
        self._lock: int | None = None

    def __enter__(self) -> JsonlLedger:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise LedgerError("Another ledger writer is active") from error
        self._lock = descriptor
        try:
            self._load()
        except (OSError, ValueError, LedgerError):
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._lock is not None:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            os.close(self._lock)
            self._lock = None

    def _load(self) -> None:
        self.events.clear()
        if not self.path.exists():
            return
        if self.path.stat().st_size > _MAX_LEDGER:
            raise LedgerError("Ledger size exceeds supported bounds")
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise LedgerError("Ledger has an incomplete final event; explicit recovery required")
        for index, line in enumerate(raw.splitlines()):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError) as error:
                raise LedgerError("Ledger event is malformed") from error
            if (
                not isinstance(row, dict)
                or row.get("seq") != index
                or not isinstance(row.get("event"), str)
            ):
                raise LedgerError("Ledger event ordering is invalid")
            self.events.append(row)

    def append(self, event: str, **data: Any) -> dict[str, Any]:
        if self._lock is None:
            raise LedgerError("Ledger writer lock is required")
        if {"event", "seq"}.intersection(data):
            raise LedgerError("Reserved ledger fields cannot be replaced")
        if event == "model_completed":
            previous = self.model_response(data["call_id"], data["request_hash"])
            if previous is not None and previous != data["response"]:
                raise LedgerError("Logical call has conflicting completed responses")
        row = {"seq": len(self.events), "event": event, **data}
        encoded = (json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(encoded) > _MAX_EVENT:
            raise LedgerError("Ledger event exceeds supported bounds")
        if self.path.exists() and self.path.stat().st_size + len(encoded) > _MAX_LEDGER:
            raise LedgerError("Ledger size exceeds supported bounds")
        created = not self.path.exists()
        descriptor = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "ab") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if created:
            _sync_directory(self.path.parent)
        self.events.append(row)
        return row

    def started(self, call_id: str) -> bool:
        return any(
            row.get("call_id") == call_id and row["event"] == "model_started" for row in self.events
        )

    def model_response(self, call_id: str, request_hash: str) -> dict[str, Any] | None:
        found: dict[str, Any] | None = None
        for row in self.events:
            if row.get("call_id") != call_id:
                continue
            if row.get("request_hash", request_hash) != request_hash:
                raise LedgerError("Logical call request changed on resume")
            if row["event"] == "model_completed":
                if found is not None and found != row["response"]:
                    raise LedgerError("Logical call has conflicting completed responses")
                found = row["response"]
        return found
