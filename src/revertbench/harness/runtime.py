"""Secret-free host adapter for original, per-case grading snapshots."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, TracebackType
from typing import Any, Self


class RuntimeFailure(RuntimeError):
    """An infrastructure failure, never a synthetic model score."""


def worker_environment() -> dict[str, str]:
    """Pass Docker transport settings, never model credentials or Python injection."""
    names = {
        "PATH",
        "HOME",
        "TMPDIR",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
        "DOCKER_CONTEXT",
    }
    environment = {name: os.environ[name] for name in names if name in os.environ}
    environment.update({"LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"})
    return environment


def _bridge_module() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "scripts/episode_runtime.py"
    spec = importlib.util.spec_from_file_location("_episode_bridge", path)
    if spec is None or spec.loader is None:
        raise RuntimeFailure("bridge_missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PinnedRuntime:
    """The model sees only tool replies, never prepare metadata or final grades."""

    def __init__(self, case_dir: Path, *, cache: Path, output: Path, episode_id: str) -> None:
        if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", episode_id) is None:
            raise ValueError("episode_id must be an opaque bounded alias")
        self.case_dir, self.cache, self.output = (
            case_dir.resolve(),
            cache.resolve(),
            output.resolve(),
        )
        self.episode_id = episode_id
        self._process: subprocess.Popen[bytes] | None = None
        self._temporary: TemporaryDirectory[str] | None = None
        self._metadata: dict[str, Any] | None = None
        self._submission: bytes | None = None
        self._sequence = 0
        self._closed = False
        self._stderr = bytearray()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if error is None:
            self.close()
        else:
            with suppress(RuntimeFailure):
                self.close()

    def _launch(self) -> None:
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeFailure("uv_missing")
        self.cache.mkdir(parents=True, exist_ok=True)
        self.output.mkdir(parents=True, exist_ok=True)
        case = json.loads((self.case_dir / "case.json").read_bytes())
        commit = case["global_suite_evidence"]["implementation_commit"]
        self._temporary = TemporaryDirectory(prefix="episode-grader-", dir=self.cache)
        snapshot = Path(self._temporary.name) / "snapshot"
        bridge = _bridge_module()
        root = Path(__file__).resolve().parents[3]
        bridge.reproduction_module()._export_grader(root, commit, snapshot)
        self._process = subprocess.Popen(
            [
                uv,
                "run",
                "--project",
                str(snapshot),
                "--frozen",
                "python",
                "-I",
                str(root / "scripts/episode_runtime.py"),
                "--case-dir",
                str(self.case_dir),
                "--pinned-root",
                str(snapshot),
                "--cache",
                str(self.cache),
                "--output",
                str(self.output),
                "--episode-id",
                self.episode_id,
            ],
            cwd=snapshot,
            env=worker_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            bufsize=0,
        )

    def _exchange(
        self, operation: str, payload: dict[str, Any], *, timeout: float = 30
    ) -> dict[str, Any]:
        process = self._process
        if (
            process is None
            or process.stdin is None
            or process.stdout is None
            or process.stderr is None
        ):
            raise RuntimeFailure("not_prepared")
        self._sequence += 1
        request = {
            "protocol": "1",
            "episode_id": self.episode_id,
            "request_id": self._sequence,
            "operation": operation,
            "payload": payload,
        }
        encoded = json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(encoded) > 524288:
            raise RuntimeFailure("request_limit")
        captured = bytearray()
        deadline = time.monotonic() + timeout
        written = 0
        try:
            os.set_blocking(process.stdin.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                while b"\n" not in captured:
                    if time.monotonic() >= deadline:
                        raise RuntimeFailure("worker_timeout")
                    for key, _ in selector.select(min(0.2, max(0, deadline - time.monotonic()))):
                        if key.data == "stdin":
                            with suppress(BlockingIOError):
                                written += os.write(key.fd, encoded[written : written + 16384])
                            if written == len(encoded):
                                selector.unregister(key.fileobj)
                            continue
                        chunk = os.read(key.fd, 16384)
                        if not chunk:
                            if key.data == "stdout":
                                raise RuntimeFailure("worker_lost")
                            selector.unregister(key.fileobj)
                        elif key.data == "stderr":
                            self._stderr.extend(chunk[: max(0, 65536 - len(self._stderr))])
                        else:
                            captured.extend(chunk)
                            if len(captured) > 8 * 1024 * 1024:
                                raise RuntimeFailure("worker_report_limit")
            if written != len(encoded):
                raise RuntimeFailure("worker_early_response")
            response = json.loads(captured)
            if (
                not isinstance(response, dict)
                or response.get("protocol") != "1"
                or response.get("episode_id") != self.episode_id
                or response.get("request_id") != self._sequence
                or response.get("operation") != operation
                or not isinstance(response.get("result"), dict)
            ):
                raise RuntimeFailure("worker_identity_mismatch")
            return dict(response["result"])
        except RuntimeFailure:
            self._terminate()
            raise
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as error:
            self._terminate()
            raise RuntimeFailure("worker_protocol_failed") from error
        finally:
            if self.output.is_dir():
                (self.output / "runtime-stderr.log").write_bytes(self._stderr)

    def prepare(self) -> dict[str, Any]:
        if self._closed:
            raise RuntimeFailure("runtime_closed")
        if self._metadata is None:
            try:
                self._launch()
                self._metadata = self._exchange("prepare", {}, timeout=900)
            except (OSError, ValueError, RuntimeError, KeyError, RecursionError) as error:
                self.close()
                raise RuntimeFailure("runtime_prepare_failed") from error
        return self._metadata

    def _require_prepared(self) -> None:
        if self._closed or self._metadata is None:
            raise RuntimeFailure("not_prepared")

    def tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_prepared()
        reply = self._exchange("tool", {"name": name, "arguments": arguments}, timeout=35)
        if len(json.dumps(reply, ensure_ascii=False).encode("utf-8")) > 8192:
            raise RuntimeFailure("tool_reply_limit")
        if name == "write_test" and reply.get("ok") is True:
            self._submission = arguments["content"].encode("utf-8")
        return reply

    def restore_submission(self, data: bytes) -> None:
        self._require_prepared()
        if not isinstance(data, bytes) or len(data) > 65536:
            raise ValueError("submission must be bounded UTF8 bytes")
        source = data.decode("utf-8")
        if self._exchange("restore", {"content": source}).get("restored") is not True:
            self._terminate()
            raise RuntimeFailure("restore_report_invalid")
        self._submission = data

    def get_submission(self) -> bytes:
        self._require_prepared()
        if self._submission is None:
            raise RuntimeFailure("no_submission")
        return self._submission

    def grade(self) -> dict[str, Any]:
        self.get_submission()
        result = self._exchange("grade", {}, timeout=180)
        if (
            not isinstance(result.get("status"), str)
            or (result.get("score") is not None and type(result.get("score")) is not int)
            or (
                result.get("status"),
                result.get("score"),
            )
            not in {
                ("passed", 1),
                ("rejected", 0),
                ("invalid_run", None),
            }
        ):
            raise RuntimeFailure("grade_report_invalid")
        return result

    def close(self) -> None:
        if self._closed:
            return
        process = self._process
        try:
            if process is not None:
                if process.poll() is not None:
                    raise RuntimeFailure("worker_cleanup_failed")
                if self._exchange("close", {}, timeout=10).get("closed") is not True:
                    raise RuntimeFailure("worker_cleanup_failed")
                try:
                    exit_code = process.wait(timeout=5)
                except subprocess.TimeoutExpired as exception:
                    raise RuntimeFailure("worker_cleanup_failed") from exception
                if exit_code != 0:
                    raise RuntimeFailure("worker_cleanup_failed")
        finally:
            self._terminate()

    def _terminate(self) -> None:
        """Never issue another request after a protocol fault or ambiguous response."""
        self._closed = True
        process = self._process
        try:
            if process is not None:
                if process.poll() is None:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
        finally:
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None
