"""Upstream suite capacity is explicit and separate from scored submissions."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import docker
import pytest
from pydantic import ValidationError

from revertbench.sandbox.agent import AgentExecResult
from revertbench.sandbox.worker import SandboxError, SandboxLimits
from revertbench.validate.baseline import BaselineResult, DockerBaselineRunner


@pytest.mark.parametrize("provided", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_baseline_client_timeout_ownership_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provided: bool, failure: bool
) -> None:
    from revertbench.validate import baseline

    client = Mock()
    client.api.timeout = 13
    factory = Mock(return_value=client)
    monkeypatch.setattr(docker, "from_env", factory)
    seen: dict[str, Any] = {}

    class Box:
        def __init__(self, image: str, tree: Path, **kwargs: Any) -> None:
            seen.update(kwargs)

        def __enter__(self) -> "Box":
            if failure:
                raise SandboxError("synthetic unavailable")
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def exec(self, argv: list[str], **kwargs: Any) -> AgentExecResult:
            assert kwargs == {"timeout_seconds": 60, "output_limit_bytes": 65536}
            payload = dict(collected=1, passed=1, node_ids=["test_one"])
            return AgentExecResult(
                status="completed",
                exit_code=0,
                stdout=b"REVERTBENCH_BASELINE=" + json.dumps(payload).encode(),
            )

    monkeypatch.setattr(baseline, "AgentSandbox", Box)
    result = DockerBaselineRunner(
        "synthetic-image", client=client if provided else None, limits=SandboxLimits(pids=256)
    )(tmp_path)
    assert result.status == ("invalid_run" if failure else "passed")
    assert result.pids_limit == 256
    assert seen["client"] is client and seen["limits"].pids == 256
    assert client.api.timeout == 13
    if provided:
        factory.assert_not_called()
        client.close.assert_not_called()
    else:
        factory.assert_called_once_with(timeout=70)
        client.close.assert_called_once_with()


def test_owned_client_startup_failure_stays_infrastructure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        docker,
        "from_env",
        Mock(side_effect=docker.errors.DockerException("synthetic unavailable")),
    )
    result = DockerBaselineRunner("synthetic-image")(tmp_path)
    assert result.status == "invalid_run" and result.reason == "baseline_sandbox_unavailable"
    assert result.pids_limit == 64


@pytest.mark.parametrize("value", [7, 257, True, "256", 256.0])
def test_baseline_report_resource_limit_is_strict_and_bounded(value: object) -> None:
    with pytest.raises(ValidationError):
        BaselineResult.model_validate({"status": "passed", "pids_limit": value})


def test_legacy_baseline_report_keeps_default_pid_limit() -> None:
    assert BaselineResult.model_validate_json('{"status":"passed"}').pids_limit == 64


def _thread_suite(tree: Path) -> None:
    (tree / "test_threads.py").write_text(
        "import threading\nfrom unittest import TestCase\n"
        "class ThreadTests(TestCase):\n"
        " def test_hundred_threads(self):\n"
        "  stop=threading.Event()\n  started=[]\n"
        "  try:\n"
        "   for _ in range(100):\n"
        "    thread=threading.Thread(target=stop.wait)\n"
        "    thread.start()\n    started.append(thread)\n"
        "   self.assertEqual(len(started),100)\n"
        "  finally:\n   stop.set()\n   for thread in started: thread.join(timeout=5)\n"
    )


@pytest.mark.docker
def test_explicit_upstream_pid_capacity_runs_bounded_hundred_thread_suite(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    _thread_suite(tmp_path)
    ordinary = DockerBaselineRunner(worker_image, client=docker_client, profile="unittest")(
        tmp_path
    )
    explicit = DockerBaselineRunner(
        worker_image, client=docker_client, profile="unittest", limits=SandboxLimits(pids=256)
    )(tmp_path)
    assert ordinary.status == "failed" and ordinary.errors == 1
    assert ordinary.pids_limit == 64
    assert explicit.status == "passed" and explicit.passed == 1
    assert explicit.pids_limit == 256


@pytest.mark.docker
def test_owned_transport_allows_quiet_suite_beyond_ten_seconds(
    tmp_path: Path, worker_image: str
) -> None:
    (tmp_path / "test_quiet.py").write_text(
        "import time\nfrom unittest import TestCase\nclass QuietTests(TestCase):\n"
        " def test_quiet(self):\n  time.sleep(11.2)\n  self.assertTrue(True)\n"
    )
    result = DockerBaselineRunner(worker_image, profile="unittest")(tmp_path)
    assert result.status == "passed", result.model_dump_json()
    assert result.collected == result.passed == 1
    assert result.pids_limit == 64
