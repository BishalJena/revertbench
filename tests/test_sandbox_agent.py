"""Agent boundary tests; hostile filesystem operations run only inside Docker."""

import io
import tarfile
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock

import docker
import pytest

from revertbench.sandbox.agent import AgentSandbox
from revertbench.sandbox.archives import SUBMISSION_NAME, ArchiveError, single_file_archive
from revertbench.sandbox.worker import SandboxError, SandboxLimits

_PYTHON = "/usr/local/bin/python"


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "repo"
    tree.mkdir()
    (tree / "api.py").write_text("VALUE = 'bad'\n", encoding="utf-8")
    (tree / ".git").mkdir()
    (tree / ".git" / "config").write_text("history")
    (tree / ".env").write_text("DUMMY_PRIVATE_VALUE=never-transfer")
    (tree / "context").mkdir()
    (tree / "context" / "private.md").write_text("private")
    return tree


def _wire(*pairs: tuple[bytes | None, bytes | None]) -> io.BytesIO:
    data = bytearray()
    for pair in pairs:
        for stream, payload in enumerate(pair, 1):
            if payload is not None:
                data.extend(bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big"))
                data.extend(payload)
    return io.BytesIO(data)


def _fake_client(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock, MagicMock]:
    client, container, volume = MagicMock(), MagicMock(), MagicMock()
    client.images.get.return_value.id = "sha256:worker"
    client.images.get.return_value.labels = {"revertbench.worker.protocol": "1"}
    container.id = "opaque-container"
    volume.name = "opaque-volume"
    client.containers.create.return_value = container
    client.api.exec_create.return_value = {"Id": "opaque-exec"}
    client.api.exec_start.return_value = _wire()
    client.api.exec_inspect.return_value = {"ExitCode": 0, "Running": False}
    monkeypatch.setattr("revertbench.sandbox.agent.seed_tree_volume", lambda *_args: volume)
    return client, container, volume


def test_shared_hardening_and_bounded_workspace_are_used(
    source_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, container, volume = _fake_client(monkeypatch)
    with AgentSandbox("worker", source_tree, client=client) as agent:
        assert agent.container_id == container.id
        options = client.containers.create.call_args.kwargs
        assert options["user"] == "10001:10001"
        assert options["network_disabled"] is True
        assert options["read_only"] is True
        assert options["cap_drop"] == ["ALL"]
        assert options["volumes"] == {volume.name: {"bind": "/work/seed", "mode": "ro"}}
        assert "size=67108864" in options["tmpfs"]["/workspace/repo"]
        assert options["entrypoint"][:3] == [_PYTHON, "-I", "-S"]
        assert "GROQ_API_KEY" not in options["environment"]
    container.remove.assert_called_once()
    volume.remove.assert_called_once()
    agent.close()
    container.remove.assert_called_once()


def test_exec_uses_argv_without_a_host_shell_and_caps_captured_bytes(
    source_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _ = _fake_client(monkeypatch)
    client.api.exec_start.side_effect = [_wire(), _wire((b"stdout", b"stderr"))]
    with AgentSandbox("worker", source_tree, client=client) as agent:
        command = [_PYTHON, "-c", "print('literal ; $(not-a-host-command)')"]
        result = agent.exec(command)
        assert result.exit_code == 0
        assert result.stdout == b"stdout"
        assert result.stderr == b"stderr"
        assert result.stdout_text == "stdout"
        assert client.api.exec_create.call_args.kwargs["cmd"] == command
        assert client.api.exec_create.call_args.kwargs["user"] == "10001:10001"
        assert client.api.exec_create.call_args.kwargs["workdir"] == "/workspace/repo"


def test_output_limit_removes_the_agent_and_preserves_only_bounded_output(
    source_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, container, volume = _fake_client(monkeypatch)
    client.api.exec_start.side_effect = [_wire(), _wire((b"too much output", None))]
    with AgentSandbox("worker", source_tree, client=client) as agent:
        result = agent.exec(["ignored"], output_limit_bytes=5)
        assert result.status == "output_limit"
        assert result.stdout == b"too m"
        assert result.exit_code is None
        assert agent.closed
    container.remove.assert_called_once()
    volume.remove.assert_called_once()


def test_wall_timeout_cleans_up_and_does_not_leave_a_live_tool(
    source_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, container, _ = _fake_client(monkeypatch)
    release = Event()

    class WaitingSocket(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            release.wait(1)
            return b""

    client.api.exec_start.side_effect = [_wire(), WaitingSocket()]
    container.remove.side_effect = lambda **_kwargs: release.set()
    with AgentSandbox("worker", source_tree, client=client) as agent:
        result = agent.exec(["sleep"], timeout_seconds=0.02)
        assert result.status == "timeout"
        assert agent.closed


def test_partial_start_failure_cleans_up_owned_resources(
    source_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, container, volume = _fake_client(monkeypatch)
    container.start.side_effect = docker.errors.APIError("sanitized by wrapper")
    with pytest.raises(SandboxError, match="start"):
        AgentSandbox("worker", source_tree, client=client).start()
    container.remove.assert_called_once()
    volume.remove.assert_called_once()


def test_untrusted_image_and_invalid_commands_are_rejected(
    source_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _ = _fake_client(monkeypatch)
    client.images.get.return_value.labels = {}
    with pytest.raises(SandboxError, match="image"):
        AgentSandbox("other", source_tree, client=client).start()
    client.images.get.return_value.labels = {"revertbench.worker.protocol": "1"}
    with AgentSandbox("worker", source_tree, client=client) as agent:
        for command in ([], [""], ["echo", "bad\x00argument"]):
            with pytest.raises(ValueError):
                agent.exec(command)


def test_submission_transport_accepts_only_the_expected_bounded_file(
    source_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _ = _fake_client(monkeypatch)
    payload = b"def test_behavior():\n    assert True\n"
    archive = single_file_archive(payload, name=SUBMISSION_NAME)
    client.api.exec_start.side_effect = [_wire(), _wire((archive, None))]
    with AgentSandbox("worker", source_tree, client=client) as agent:
        assert agent.submission_bytes() == payload
        command = client.api.exec_create.call_args.kwargs["cmd"]
        assert command[:3] == [_PYTHON, "-I", "-S"]


def test_submission_extraction_failure_is_not_an_infrastructure_pass(
    source_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _ = _fake_client(monkeypatch)
    invalid = io.BytesIO()
    with tarfile.open(fileobj=invalid, mode="w") as archive:
        member = tarfile.TarInfo(SUBMISSION_NAME)
        member.type = tarfile.SYMTYPE
        member.linkname = "somewhere"
        archive.addfile(member)
    client.api.exec_start.side_effect = [_wire(), _wire((invalid.getvalue(), None))]
    with AgentSandbox("worker", source_tree, client=client) as agent, pytest.raises(ArchiveError):
        agent.submission_bytes()


@pytest.mark.docker
def test_agent_is_nonroot_without_history_network_secrets_or_host_mounts(
    source_tree: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    code = (
        "import os,pathlib,socket; p=pathlib.Path('/workspace/repo'); "
        "assert os.getuid()==10001; assert not (p/'.git').exists(); "
        "assert not (p/'.env').exists(); assert not (p/'context').exists(); "
        "assert 'GROQ_API_KEY' not in os.environ; "
        "assert not pathlib.Path('/var/run/docker.sock').exists(); "
        "s=socket.socket(); s.settimeout(.3)\n"
        "try: s.connect(('1.1.1.1',443))\n"
        "except OSError: print('isolated')\n"
        "else: raise AssertionError('network reachable')\n"
    )
    with AgentSandbox(worker_image, source_tree, client=docker_client) as agent:
        result = agent.exec([_PYTHON, "-I", "-S", "-c", code])
        assert result.exit_code == 0, result.stderr_text
        assert result.stdout == b"isolated\n"
        protected = agent.exec(
            [
                _PYTHON,
                "-I",
                "-S",
                "-c",
                "from pathlib import Path; Path('/etc/agent-probe').write_text('forbidden')",
            ]
        )
        assert protected.exit_code != 0


@pytest.mark.docker
def test_only_submission_bytes_cross_after_agent_edits(
    source_tree: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    payload = "def test_behavior():\n    assert True\n"
    edited_source = 'VALUE = "edited"'
    code = (
        "from pathlib import Path; p=Path('/workspace/repo'); "
        f"(p/'api.py').write_text({edited_source!r}); "
        "(p/'tests').mkdir(); (p/'tests'/'conftest.py').write_text('raise RuntimeError()'); "
        f"(p/'tests'/{SUBMISSION_NAME!r}).write_text({payload!r})"
    )
    with AgentSandbox(worker_image, source_tree, client=docker_client) as agent:
        result = agent.exec([_PYTHON, "-I", "-S", "-c", code])
        assert result.exit_code == 0, result.stderr_text
        assert agent.submission_bytes() == payload.encode()
    assert (source_tree / "api.py").read_text() == "VALUE = 'bad'\n"
    assert not (source_tree / "tests").exists()


@pytest.mark.parametrize(
    "kind", ["parent_symlink", "file_symlink", "hardlink", "invalid_utf8", "oversize"]
)
@pytest.mark.docker
def test_submission_filesystem_escapes_and_invalid_sources_are_rejected(
    source_tree: Path, docker_client: docker.DockerClient, worker_image: str, kind: str
) -> None:
    setup = "import os; from pathlib import Path; p=Path('/workspace/repo'); "
    if kind == "parent_symlink":
        setup += (
            "Path('/tmp/elsewhere').mkdir(); "
            f"Path('/tmp/elsewhere/{SUBMISSION_NAME}').write_text('pass'); "
            "(p/'tests').symlink_to('/tmp/elsewhere')"
        )
    else:
        setup += "(p/'tests').mkdir(); target=p/'tests'/" + repr(SUBMISSION_NAME) + "; "
        if kind == "file_symlink":
            setup += "Path('/tmp/other.py').write_text('pass'); target.symlink_to('/tmp/other.py')"
        elif kind == "hardlink":
            setup += "(p/'other.py').write_text('pass'); os.link(p/'other.py', target)"
        elif kind == "invalid_utf8":
            setup += "target.write_bytes(b'\\xff')"
        else:
            setup += "target.write_bytes(b'x'*65537)"
    with AgentSandbox(worker_image, source_tree, client=docker_client) as agent:
        result = agent.exec([_PYTHON, "-I", "-S", "-c", setup])
        assert result.exit_code == 0, result.stderr_text
        with pytest.raises(ArchiveError):
            agent.submission_bytes()


@pytest.mark.docker
def test_agent_wall_and_output_limits_remove_containers(
    source_tree: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    with AgentSandbox(worker_image, source_tree, client=docker_client) as agent:
        result = agent.exec(
            [_PYTHON, "-I", "-S", "-c", "import time; time.sleep(10)"], timeout_seconds=1
        )
        assert result.status == "timeout"
        assert agent.closed
    with AgentSandbox(worker_image, source_tree, client=docker_client) as agent:
        result = agent.exec(
            [_PYTHON, "-I", "-S", "-c", "print('x'*1000000)"], output_limit_bytes=1000
        )
        assert result.status == "output_limit"
        assert len(result.stdout) + len(result.stderr) == 1000
        assert agent.closed


@pytest.mark.docker
def test_agent_workspace_is_memory_backed_and_bounded(
    source_tree: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    limits = SandboxLimits(tmpfs_bytes=1_048_576)
    with AgentSandbox(worker_image, source_tree, client=docker_client, limits=limits) as agent:
        result = agent.exec(
            [
                _PYTHON,
                "-I",
                "-S",
                "-c",
                "from pathlib import Path\n"
                "try: Path('/workspace/repo/large').write_bytes(b'x'*2000000)\n"
                "except OSError: print('bounded')\n"
                "else: raise AssertionError('workspace unbounded')",
            ]
        )
        assert result.exit_code == 0, result.stderr_text
        assert result.stdout == b"bounded\n"


def test_exec_waits_for_docker_exit_status_after_stream_closes(
    source_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _ = _fake_client(monkeypatch)
    client.api.exec_inspect.side_effect = [
        {"Running": True, "ExitCode": None},
        {"Running": False, "ExitCode": 0},
    ]
    with AgentSandbox("worker", source_tree, client=client):
        assert client.api.exec_inspect.call_count == 2


@pytest.mark.docker
def test_submission_fifo_is_rejected_without_blocking_or_destroying_agent(
    source_tree: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    code = (
        "import os; from pathlib import Path; p=Path('/workspace/repo/tests'); p.mkdir(); "
        f"os.mkfifo(p/{SUBMISSION_NAME!r})"
    )
    with AgentSandbox(worker_image, source_tree, client=docker_client) as agent:
        assert agent.exec([_PYTHON, "-I", "-S", "-c", code]).exit_code == 0
        with pytest.raises(ArchiveError):
            agent.submission_bytes()
        assert not agent.closed
        assert agent.exec([_PYTHON, "-I", "-S", "-c", "print('alive')"]).stdout == b"alive\n"


@pytest.mark.docker
def test_submission_read_keeps_directory_fds_during_concurrent_parent_swaps(
    source_tree: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    valid = b"def test_snapshot():\n    assert True\n"
    race = (
        "import os,time; from pathlib import Path; p=Path('/workspace/repo'); "
        "deadline=time.monotonic()+3\n"
        "while time.monotonic()<deadline:\n"
        "    try:\n"
        "        os.rename(p/'tests',p/'held-tests')\n"
        "        os.symlink('/tmp/escape',p/'tests')\n"
        "        time.sleep(.002)\n"
        "        os.unlink(p/'tests')\n"
        "        os.rename(p/'held-tests',p/'tests')\n"
        "        time.sleep(.002)\n"
        "    except OSError: pass\n"
    )
    setup = (
        "import subprocess; from pathlib import Path; p=Path('/workspace/repo'); "
        "(p/'tests').mkdir(); Path('/tmp/escape').mkdir(); "
        f"(p/'tests'/{SUBMISSION_NAME!r}).write_bytes({valid!r}); "
        f"Path('/tmp/escape/{SUBMISSION_NAME}').write_text('ESCAPED'); "
        f"subprocess.Popen([{_PYTHON!r},'-I','-S','-c',{race!r}], "
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
    )
    with AgentSandbox(worker_image, source_tree, client=docker_client) as agent:
        assert agent.exec([_PYTHON, "-I", "-S", "-c", setup]).exit_code == 0
        for _ in range(12):
            try:
                captured = agent.submission_bytes()
            except ArchiveError:
                continue
            assert captured == valid
        assert not agent.closed


def test_cleanup_failure_is_explicit_and_a_retry_is_idempotent(
    source_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, container, volume = _fake_client(monkeypatch)
    agent = AgentSandbox("worker", source_tree, client=client).start()
    container.remove.side_effect = [docker.errors.APIError("cannot remove"), None]
    volume.remove.side_effect = [docker.errors.APIError("still mounted"), None]
    with pytest.raises(SandboxError, match="cleanup"):
        agent.close()
    agent.close()
    agent.close()
    assert container.remove.call_count == 2
    assert volume.remove.call_count == 2


def test_exec_transport_failure_is_sanitized_and_cleans_owned_resources(
    source_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, container, volume = _fake_client(monkeypatch)
    client.api.exec_start.side_effect = docker.errors.APIError("sensitive-credential-example")
    with pytest.raises(SandboxError) as error:
        AgentSandbox("worker", source_tree, client=client).start()
    assert "sensitive-credential-example" not in str(error.value)
    container.remove.assert_called_once()
    volume.remove.assert_called_once()


def test_truncated_exec_frame_is_not_accepted_as_success(
    source_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _ = _fake_client(monkeypatch)
    client.api.exec_start.side_effect = [_wire(), io.BytesIO(b"\x01\0\0\0\0\0\0\x05short"[:-2])]
    with AgentSandbox("worker", source_tree, client=client) as agent:
        with pytest.raises(SandboxError, match="truncated"):
            agent.exec(["ignored"])
        assert agent.closed
