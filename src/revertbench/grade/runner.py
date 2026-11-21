"""Bounded local execution of trusted fixtures; this backend is not a sandbox."""

import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from revertbench.schema import ExecutionResult

_REPORT_LIMIT = 1_048_576
_SUBMISSION_PATH = Path("tests/test_revertbench_submission.py")


@dataclass(frozen=True)
class _Captured:
    exit_code: int
    stdout: str
    stderr: str
    stopped: Literal["timeout", "output"] | None = None
    truncated: bool = False


def _bounded_text(streams: dict[str, bytearray], limit: int) -> tuple[str, str, bool]:
    remaining = limit
    results: list[str] = []
    truncated = False
    for name in ("stdout", "stderr"):
        encoded = streams[name].decode("utf-8", errors="replace").encode("utf-8")
        truncated |= len(encoded) > remaining
        text = encoded[:remaining].decode("utf-8", errors="ignore")
        results.append(text)
        remaining -= len(text.encode("utf-8"))
    return results[0], results[1], truncated


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        if process.poll() is None:
            raise


def _read_output(
    process: subprocess.Popen[bytes], timeout_seconds: float, output_limit_bytes: int
) -> _Captured:
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    stopped: Literal["timeout", "output"] | None = None
    deadline = time.monotonic() + timeout_seconds
    assert process.stdout is not None and process.stderr is not None
    with selectors.DefaultSelector() as selector:
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stopped = "timeout"
                break
            for key, _ in selector.select(min(remaining, 0.1)):
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                room = output_limit_bytes - sum(len(value) for value in streams.values())
                streams[key.data].extend(chunk[:room])
                if len(chunk) > room:
                    stopped = "output"
                    break
            if stopped is not None:
                break
    if stopped is not None:
        _kill_group(process)
    try:
        exit_code = process.wait(timeout=max(deadline - time.monotonic(), 0.01))
    except subprocess.TimeoutExpired:
        stopped = "timeout"
        _kill_group(process)
        exit_code = process.wait(timeout=2)
    stdout, stderr, truncated = _bounded_text(streams, output_limit_bytes)
    return _Captured(exit_code, stdout, stderr, stopped, truncated or stopped == "output")


def _environment(tree: Path, report: Path, seed: int) -> dict[str, str]:
    trusted_src = Path(__file__).resolve().parents[2]
    return {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONPATH": os.pathsep.join(map(str, (trusted_src, tree / "src", tree))),
        "PYTHONHASHSEED": str(seed),
        "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_ADDOPTS": "",
        "REVERTBENCH_TREE": str(tree),
        "REVERTBENCH_REPORT": str(report),
        "REVERTBENCH_SEED": str(seed),
    }


def _command(tree: Path, config: Path) -> list[str]:
    return [
        sys.executable,
        "-P",
        "-m",
        "pytest",
        "-q",
        "-s",
        "--disable-warnings",
        "-c",
        str(config),
        "--rootdir",
        str(tree),
        "--noconftest",
        "-p",
        "no:cacheprovider",
        "-p",
        "revertbench.grade._pytest_plugin",
        str(tree / _SUBMISSION_PATH),
    ]


def _consistent(result: ExecutionResult, exit_code: int, seed: int) -> bool:
    collected = result.collected_node_ids
    order = result.execution_order
    if result.status != "completed" or result.seed != seed or result.exit_code != exit_code:
        return False
    if len(set(collected)) != len(collected) or len(set(order)) != len(order):
        return False
    if not set(order) <= set(collected):
        return False
    if result.collection_errors:
        return not result.tests and not order
    if set(order) != set(collected) or len(result.tests) != len(order):
        return False
    if {test.node_id for test in result.tests} != set(order):
        return False
    return all(
        test.setup is not None
        and test.teardown is not None
        and (test.setup.outcome != "passed" or test.call is not None)
        for test in result.tests
    )


def _decode_report(report: Path, captured: _Captured, seed: int) -> ExecutionResult:
    started = report.with_suffix(".started").is_file()
    common = {
        "stdout": captured.stdout,
        "stderr": captured.stderr,
        "exit_code": captured.exit_code,
        "truncated": captured.truncated,
    }
    if captured.stopped is not None:
        status = (
            "submission_timeout" if captured.stopped == "timeout" else "submission_output_limit"
        )
        if not started:
            status = "invalid_run"
        return ExecutionResult.model_validate(
            {
                "status": status,
                "seed": seed,
                "reason": status,
                **common,
            }
        )
    if captured.exit_code in (3, 4) or captured.exit_code < 0:
        reason = "pytest_internal_error" if captured.exit_code in (3, 4) else "worker_lost"
        return ExecutionResult.model_validate(
            {"status": "invalid_run", "seed": seed, "reason": reason, **common}
        )
    if not report.is_file():
        return ExecutionResult.model_validate(
            {
                "status": "submission_error" if started else "invalid_run",
                "seed": seed,
                "reason": "incomplete_report" if started else "worker_startup_failed",
                **common,
            }
        )
    if report.stat().st_size > _REPORT_LIMIT:
        return ExecutionResult.model_validate(
            {
                "status": "submission_error",
                "seed": seed,
                "reason": "report_limit",
                **common,
            }
        )
    try:
        result = ExecutionResult.model_validate_json(report.read_bytes())
    except (ValidationError, ValueError):
        return ExecutionResult.model_validate(
            {
                "status": "submission_error",
                "seed": seed,
                "reason": "malformed_report",
                **common,
            }
        )
    if not _consistent(result, captured.exit_code, seed):
        return ExecutionResult.model_validate(
            {
                "status": "submission_error",
                "seed": seed,
                "reason": "inconsistent_report",
                **common,
            }
        )
    return result.model_copy(update=common)


def _execute(
    tree: Path, config: Path, report: Path, seed: int, timeout: float, limit: int
) -> ExecutionResult:
    with subprocess.Popen(
        _command(tree, config),
        cwd=tree,
        env=_environment(tree, report, seed),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as process:
        try:
            captured = _read_output(process, timeout, limit)
        finally:
            _kill_group(process)
    return _decode_report(report, captured, seed)


def run_tree(
    tree: Path,
    submission: str,
    *,
    seed: int,
    timeout_seconds: float = 10,
    output_limit_bytes: int = 65536,
) -> ExecutionResult:
    """Execute an explicitly trusted toy submission in a fresh copy, never as security isolation."""
    if not tree.is_dir():
        return ExecutionResult(status="invalid_run", seed=seed, reason="tree_unavailable")
    if timeout_seconds <= 0 or output_limit_bytes < 1 or not 0 <= seed <= 4_294_967_295:
        return ExecutionResult(status="invalid_run", seed=seed, reason="invalid_limits")
    if (tree / _SUBMISSION_PATH).exists():
        return ExecutionResult(status="invalid_run", seed=seed, reason="reserved_path_collision")
    try:
        if tree.is_symlink() or any(path.is_symlink() for path in tree.rglob("*")):
            return ExecutionResult(status="invalid_run", seed=seed, reason="tree_symlink")
        with tempfile.TemporaryDirectory(prefix="revertbench-grade-") as scratch:
            root = Path(scratch)
            copied = root / "tree"
            shutil.copytree(
                tree, copied, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv")
            )
            destination = copied / _SUBMISSION_PATH
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(submission, encoding="utf-8")
            config = root / "pytest.ini"
            config.write_text("[pytest]\naddopts =\n", encoding="utf-8")
            return _execute(
                copied, config, root / "report.json", seed, timeout_seconds, output_limit_bytes
            )
    except (OSError, subprocess.TimeoutExpired):
        return ExecutionResult(status="invalid_run", seed=seed, reason="worker_io_failure")
