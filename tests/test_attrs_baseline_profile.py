"""Native attrs baseline environment and reporting stay separate from scored tests."""

import io
import json
import tarfile
from pathlib import Path
from unittest.mock import Mock

import docker
import pytest

from revertbench.sandbox.agent import AgentExecResult, AgentSandbox
from revertbench.sandbox.image import worker_build_context
from revertbench.sandbox.worker import SandboxLimits, hardened_container_options
from revertbench.validate.baseline import (
    BaselineProfile,
    BaselineResult,
    DockerBaselineRunner,
    baseline_command,
    parse_baseline_output,
)

PROFILE: BaselineProfile = "attrs_field_transformer_default_behavior_v1"


def test_attrs_uses_only_fixed_native_config_and_plugins() -> None:
    command = baseline_command(PROFILE)
    assert command[:3] == ("/opt/revertbench/attrs/runtime/bin/python", "-m", "pytest")
    assert command[3:7] == ("-n", "1", "-c", "/workspace/repo/pyproject.toml")
    assert {"xdist.plugin", "pytest_mypy_plugins.collect"} <= set(command)
    assert not any(flag.startswith(("-k", "--ignore", "--deselect")) for flag in command)
    assert "tests" not in command


def test_attrs_baseline_has_fixed_memory_and_standard_noexec_tmpfs() -> None:
    default = SandboxLimits()
    ordinary = DockerBaselineRunner("worker").limits
    attrs = DockerBaselineRunner("worker", profile=PROFILE).limits
    assert ordinary == default
    assert attrs.memory_bytes == 896 * 1024**2
    assert attrs.pids == default.pids
    assert attrs.cpus == default.cpus
    assert attrs.tmpfs_bytes == default.tmpfs_bytes
    assert (
        hardened_container_options(attrs)["tmpfs"] == hardened_container_options(default)["tmpfs"]
    )


@pytest.mark.parametrize(
    ("profile", "command_timeout"), [(PROFILE, 150), ("pytest", 60), ("unittest", 60)]
)
def test_baseline_timeout_is_scoped_to_attrs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: BaselineProfile, command_timeout: int
) -> None:
    executions: list[tuple[list[str], float, int]] = []
    transport_timeouts: list[int] = []
    client = Mock()

    def connect(*, timeout: int) -> Mock:
        transport_timeouts.append(timeout)
        return client

    def execute(
        box: AgentSandbox, command: list[str], timeout: float, limit: int
    ) -> AgentExecResult:
        executions.append((command, timeout, limit))
        return AgentExecResult("timeout", None)

    monkeypatch.setattr(docker, "from_env", connect)
    monkeypatch.setattr(AgentSandbox, "start", lambda box: box)
    monkeypatch.setattr(AgentSandbox, "close", lambda box: None)
    monkeypatch.setattr(AgentSandbox, "_execute", execute)

    result = DockerBaselineRunner("worker", profile=profile)(tmp_path)

    assert transport_timeouts == [command_timeout + 10]
    assert len(executions) == 1
    command, timeout, limit = executions[0]
    assert command[:3] == ["/usr/local/bin/python", "-I", "-c"]
    assert ("from revertbench._attrs_baseline import run_native_attrs" in command[3]) == (
        profile == PROFILE
    )
    assert timeout == command_timeout
    assert limit == 65_536
    assert result.status == "invalid_run"
    assert result.reason == "baseline_timeout"
    client.close.assert_called_once_with()


def test_attrs_deadline_is_not_available_for_regular_commands(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Command limits must be finite and bounded"):
        AgentSandbox("worker", tmp_path).exec(["/usr/local/bin/python", "-V"], timeout_seconds=150)


def test_attrs_mock_pass_without_original_selection_is_invalid() -> None:
    mock = BaselineResult(
        status="passed",
        profile=PROFILE,
        command=baseline_command(PROFILE),
        collected=1,
        passed=1,
        node_ids=("tests/test_attrs.py::test_one",),
    )
    report = b"REVERTBENCH_BASELINE=" + json.dumps(mock.model_dump(mode="json")).encode()
    output = parse_baseline_output(report, b"", 0, expected_profile=PROFILE)
    assert output.status == "invalid_run"
    assert output.reason == "baseline_report_inconsistent"


def test_attrs_stage_uses_separate_fixed_hash_requirements() -> None:
    root = Path(__file__).resolve().parents[1]
    context = worker_build_context(root, "python@sha256:" + "a" * 64, b"global requirements")
    with tarfile.open(fileobj=io.BytesIO(context)) as archive:
        files = {}
        for name in ("Dockerfile", "requirements.txt", "attrs-build.txt", "attrs-runtime.txt"):
            fd = archive.extractfile(name)
            assert fd is not None
            files[name] = fd.read()

    assert files["requirements.txt"] == b"global requirements"
    assert b"venv /opt/revertbench/attrs/build" in files["Dockerfile"]
    assert b"venv /opt/revertbench/attrs/runtime" in files["Dockerfile"]
    assert b"pytest==8.3.4 --hash=sha256:" in files["attrs-runtime.txt"]
    assert b"pytest-mypy-plugins==3.1.2 --hash=sha256:" in files["attrs-runtime.txt"]
    assert b"attrs==" not in files["attrs-runtime.txt"]
    assert b"hatchling==1.27.0 --hash=sha256:" in files["attrs-build.txt"]
    assert all(b" --hash=sha256:" in line for line in files["attrs-runtime.txt"].splitlines())
