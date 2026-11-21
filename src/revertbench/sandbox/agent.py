"""Mutable bad-tree workspaces in bounded, non-root Docker agents."""

from __future__ import annotations

import math
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

import docker
import requests
from docker.models.containers import Container
from docker.models.volumes import Volume

from revertbench.sandbox.archives import (
    MAX_SUBMISSION_BYTES,
    ArchiveError,
    archive_tree,
    extract_submission,
)
from revertbench.sandbox.worker import (
    SandboxError,
    SandboxLimits,
    hardened_container_options,
    remove_container,
    remove_volume,
    seed_tree_volume,
)

_PYTHON = "/usr/local/bin/python"
_WORKSPACE = "/workspace/repo"
_SEED = "/work/seed"
_ERRORS = (docker.errors.DockerException, requests.exceptions.RequestException, OSError)
_ENVIRONMENT = {
    "HOME": "/tmp",
    "LANG": "C.UTF-8",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    "PYTEST_ADDOPTS": "",
    "PYTHONPATH": f"{_WORKSPACE}/src:{_WORKSPACE}",
}
_COPY_SCRIPT = (
    "import os, shutil; "
    f"shutil.copytree({_SEED!r}, {_WORKSPACE!r}, dirs_exist_ok=True); "
    f"os.chmod({_WORKSPACE!r}, 0o755)"
)
_READ_SUBMISSION = r"""
import io
import os
import stat
import sys
import tarfile

fds = []
try:
    root = os.open('/workspace/repo', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fds.append(root)
    tests = os.open('tests', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
    fds.append(tests)
    source = os.open(
        'test_revertbench_submission.py',
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=tests,
    )
    fds.append(source)
    metadata = os.fstat(source)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > 65536:
        sys.exit(2)
    data = bytearray()
    while len(data) <= 65536:
        chunk = os.read(source, min(16384, 65537 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    if len(data) > 65536 or os.fstat(source).st_nlink != 1:
        sys.exit(2)
    data.decode('utf-8')
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        member = tarfile.TarInfo('test_revertbench_submission.py')
        member.size = len(data)
        member.mode = 0o444
        member.mtime = 0
        member.uid = member.gid = 10001
        archive.addfile(member, io.BytesIO(data))
    sys.stdout.buffer.write(output.getvalue())
except (OSError, UnicodeError, ValueError):
    sys.exit(2)
finally:
    for descriptor in reversed(fds):
        os.close(descriptor)
"""


@dataclass(frozen=True)
class AgentExecResult:
    status: Literal["completed", "timeout", "output_limit"]
    exit_code: int | None
    stdout: bytes = b""
    stderr: bytes = b""

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


@dataclass
class _Capture:
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    done: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    reason: Literal["timeout", "output_limit"] | None = None
    exit_code: int | None = None
    started: bool = False
    stream_complete: bool = False
    failed: bool = False
    failure_kind: str | None = None

    def append(self, pair: object, limit: int) -> None:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise SandboxError("agent_exec_stream_invalid")
        with self.lock:
            for destination, part in zip((self.stdout, self.stderr), pair, strict=True):
                if part is None:
                    continue
                if not isinstance(part, bytes):
                    raise SandboxError("agent_exec_stream_invalid")
                available = limit - len(self.stdout) - len(self.stderr)
                destination.extend(part[:available])
                if len(part) > available:
                    self.reason = "output_limit"
                    self.stop.set()
                    return


def _read_exact(socket: object, count: int, *, eof_allowed: bool = False) -> bytes:
    read = getattr(socket, "recv", None) or getattr(socket, "read", None)
    if not callable(read):
        raise SandboxError("agent_exec_socket_invalid")
    output = bytearray()
    while len(output) < count:
        chunk = read(count - len(output))
        if not isinstance(chunk, bytes) or len(chunk) > count - len(output):
            raise SandboxError("agent_exec_stream_invalid")
        if not chunk:
            if not output and eof_allowed:
                return b""
            raise SandboxError("agent_exec_stream_truncated")
        output.extend(chunk)
    return bytes(output)


def _read_frames(socket: object, capture: _Capture, limit: int) -> None:
    while not capture.stop.is_set():
        header = _read_exact(socket, 8, eof_allowed=True)
        if not header:
            return
        if header[0] not in {1, 2} or header[1:4] != b"\0\0\0":
            raise SandboxError("agent_exec_stream_invalid")
        remaining = int.from_bytes(header[4:8], "big")
        while remaining and not capture.stop.is_set():
            data = _read_exact(socket, min(remaining, 16_384))
            capture.append((data, None) if header[0] == 1 else (None, data), limit)
            remaining -= len(data)


class AgentSandbox:
    """A bad-tree-only workspace with bounded writes, tools and submission extraction.

    Use the trusted image built for grading. A fresh readonly Docker-managed
    volume contains only canonical case source, copied into a bounded tmpfs.
    Only the selected test's byte snapshot crosses into fresh grading workers.
    """

    def __init__(
        self,
        image: str,
        tree: Path,
        *,
        client: docker.DockerClient | None = None,
        limits: SandboxLimits | None = None,
    ) -> None:
        self.image, self.tree = image, tree
        self.client = client
        self.limits = limits or SandboxLimits()
        self._owns_client = client is None
        self._container: Container | None = None
        self._volume: Volume | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def container_id(self) -> str | None:
        return self._container.id if self._container is not None else None

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def start(self) -> Self:
        if self._closed:
            raise SandboxError("agent_closed")
        if self._container is not None:
            return self
        source = archive_tree(self.tree, writable=True)
        try:
            self.client = self.client or docker.from_env(timeout=10)
            image = self.client.images.get(self.image)
            image_id = image.id
            if image.labels.get("revertbench.worker.protocol") != "1" or not image_id:
                raise SandboxError("agent_image_untrusted")
            self._volume = seed_tree_volume(self.client, image_id, source, self.limits)
            self._container = self.client.containers.create(
                image_id,
                entrypoint=[_PYTHON, "-I", "-S", "-c"],
                command=["import time; time.sleep(86400)"],
                working_dir=_WORKSPACE,
                environment=_ENVIRONMENT,
                volumes={self._volume.name: {"bind": _SEED, "mode": "ro"}},
                labels={"revertbench.managed": "true", "revertbench.role": "agent"},
                **self._options(),
            )
            self._container.start()
            copied = self._execute([_PYTHON, "-I", "-S", "-c", _COPY_SCRIPT], 10, 1024)
            if copied.status != "completed" or copied.exit_code != 0:
                raise SandboxError("agent_source_copy_failed")
        except (SandboxError, *_ERRORS) as error:
            self.close()
            if isinstance(error, SandboxError):
                raise
            raise SandboxError("agent_start_failed") from error
        return self

    def _options(self) -> dict[str, Any]:
        options = hardened_container_options(self.limits)
        tmpfs = dict(options["tmpfs"])
        tmpfs[_WORKSPACE] = (
            f"rw,noexec,nosuid,nodev,size={self.limits.tmpfs_bytes},"
            f"uid={self.limits.uid},gid={self.limits.gid},mode=700"
        )
        options["tmpfs"] = tmpfs
        return options

    def close(self) -> None:
        """Attempt all owned-resource cleanup; success is idempotent, failure is explicit."""
        self._closed = True
        cleaned = True
        if self._container is not None:
            if remove_container(self._container):
                self._container = None
            else:
                cleaned = False
        if self._volume is not None:
            if remove_volume(self._volume):
                self._volume = None
            else:
                cleaned = False
        if self._owns_client and self.client is not None:
            self.client.close()
            self.client = None
        if not cleaned:
            raise SandboxError("agent_cleanup_failed")

    def exec(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float = 10,
        output_limit_bytes: int = 65_536,
    ) -> AgentExecResult:
        """Run argv inside the container; no host shell or arbitrary host environment."""
        if (
            isinstance(command, str)
            or not command
            or len(command) > 256
            or any(not isinstance(arg, str) or "\x00" in arg for arg in command)
            or not command[0]
        ):
            raise ValueError("Command must be a nonempty bounded argv without NUL bytes")
        try:
            length = sum(len(arg.encode("utf-8")) for arg in command)
        except UnicodeError as error:
            raise ValueError("Command arguments must be valid UTF-8") from error
        if (
            length > 65_536
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 60
            or type(output_limit_bytes) is not int
            or not 1 <= output_limit_bytes <= 65_536
        ):
            raise ValueError("Command limits must be finite and bounded")
        return self._execute(list(command), timeout_seconds, output_limit_bytes)

    def _execute(self, command: list[str], timeout: float, limit: int) -> AgentExecResult:
        if self._closed or self._container is None or self.client is None:
            raise SandboxError("agent_not_running")
        capture = _Capture()
        container_id = self._container.id
        if not container_id:
            raise SandboxError("agent_container_id_invalid")
        reader = threading.Thread(
            target=self._read_exec,
            args=(self.client.api, container_id, command, limit, capture),
            daemon=True,
            name="revertbench-agent-output",
        )
        reader.start()
        finished = capture.done.wait(timeout)
        if not finished:
            with capture.lock:
                capture.reason = "timeout"
                capture.stop.set()
                started = capture.started
            self.close()
            reader.join(timeout=1)
            if not started or capture.stream_complete:
                raise SandboxError("agent_exec_start_timeout")
        elif capture.reason is not None or capture.failed:
            self.close()
        if capture.failed and capture.reason is None:
            raise SandboxError(f"agent_exec_transport_failed:{capture.failure_kind}")
        with capture.lock:
            return AgentExecResult(
                status=capture.reason or "completed",
                exit_code=capture.exit_code if capture.reason is None else None,
                stdout=bytes(capture.stdout),
                stderr=bytes(capture.stderr),
            )

    def _read_exec(
        self,
        api: docker.APIClient,
        container_id: str,
        command: list[str],
        limit: int,
        capture: _Capture,
    ) -> None:
        output: object | None = None
        try:
            created = api.exec_create(
                container_id,
                cmd=command,
                stdout=True,
                stderr=True,
                stdin=False,
                tty=False,
                privileged=False,
                user=f"{self.limits.uid}:{self.limits.gid}",
                environment=_ENVIRONMENT,
                workdir=_WORKSPACE,
            )
            identifier = created["Id"]
            if not isinstance(identifier, str):
                raise SandboxError("agent_exec_id_invalid")
            output = api.exec_start(identifier, socket=True)
            capture.started = True
            _read_frames(output, capture, limit)
            capture.stream_complete = not capture.stop.is_set()
            while not capture.stop.is_set():
                state = api.exec_inspect(identifier)
                if state.get("Running"):
                    capture.stop.wait(0.005)
                    continue
                if type(state.get("ExitCode")) is not int:
                    raise SandboxError("agent_exec_result_invalid")
                capture.exit_code = state["ExitCode"]
                break
        except (SandboxError, KeyError, TypeError, ValueError, *_ERRORS) as error:
            capture.failed = True
            capture.failure_kind = (
                str(error) if isinstance(error, SandboxError) else type(error).__name__
            )
        finally:
            close = getattr(output, "close", None)
            if callable(close):
                try:
                    close()
                except (ValueError, *_ERRORS):
                    capture.failed = True
            capture.done.set()

    def submission_bytes(self) -> bytes:
        """Read a regular UTF-8 file using no-follow directory FDs and a bounded tar.

        The FD fixes the selected inode during reading, not its semantic contents;
        grading consumes exactly the returned bytes, never the mutable agent tree.
        """
        result = self._execute(
            [_PYTHON, "-I", "-S", "-c", _READ_SUBMISSION],
            10,
            MAX_SUBMISSION_BYTES + 16_384,
        )
        if result.status != "completed" or result.exit_code == 2:
            raise ArchiveError("Submission path, type, encoding or size is invalid")
        if result.exit_code != 0:
            raise SandboxError("agent_submission_read_failed")
        return extract_submission(result.stdout)
