"""Docker worker boundaries and lifecycle failures are explicit grading evidence."""

from pathlib import Path
from unittest.mock import Mock

import docker
import pytest

from revertbench.sandbox.worker import (
    DockerRunner,
    SandboxLimits,
    hardened_container_options,
    remove_container,
    remove_volume,
    seed_tree_volume,
)
from revertbench.schema import ExecutionResult


def test_default_options_disable_host_access_and_bound_resources() -> None:
    limits = SandboxLimits()
    options = hardened_container_options(limits)
    assert options["user"] == "10001:10001"
    assert options["read_only"] is True
    assert options["network_disabled"] is True
    assert options["network_mode"] == "none"
    assert options["cap_drop"] == ["ALL"]
    assert options["security_opt"] == ["no-new-privileges:true"]
    assert options["mem_limit"] == options["memswap_limit"] == limits.memory_bytes
    assert options["pids_limit"] == limits.pids
    assert options["nano_cpus"] == int(limits.cpus * 1_000_000_000)
    assert options["use_config_proxy"] is False
    assert options["init"] is True
    assert "volumes" not in options and "mounts" not in options
    assert set(options["tmpfs"]) == {"/run", "/tmp"}


@pytest.mark.parametrize(
    "field,value", [("memory_bytes", 0), ("pids", 0), ("cpus", 0), ("tmpfs_bytes", 0)]
)
def test_resource_limits_cannot_be_disabled(field: str, value: int) -> None:
    with pytest.raises(ValueError):
        SandboxLimits(**{field: value})


def test_seed_helper_never_starts_and_is_removed() -> None:
    client = Mock(spec=docker.DockerClient)
    volume = client.volumes.create.return_value
    volume.name = "revertbench-test-volume"
    helper = client.containers.create.return_value
    helper.put_archive.return_value = True
    volume_remove = volume.remove
    assert seed_tree_volume(client, "sha256:worker", b"canonical-tar", SandboxLimits()) is volume
    helper.start.assert_not_called()
    helper.put_archive.assert_called_once_with("/work/tree", b"canonical-tar")
    helper.remove.assert_called_once_with(force=True, v=True)
    volume_remove.assert_not_called()


def test_failed_archive_seed_cleans_helper_and_volume() -> None:
    from revertbench.sandbox.worker import SandboxError

    client = Mock(spec=docker.DockerClient)
    helper = client.containers.create.return_value
    helper.put_archive.side_effect = docker.errors.APIError("synthetic transfer failure")
    with pytest.raises(SandboxError):
        seed_tree_volume(client, "sha256:worker", b"canonical-tar", SandboxLimits())
    helper.remove.assert_called_once_with(force=True, v=True)
    client.volumes.create.return_value.remove.assert_called_once_with(force=True)


def test_cleanup_failures_are_not_reported_as_success() -> None:
    container = Mock()
    container.remove.side_effect = docker.errors.APIError("synthetic cleanup failure")
    volume = Mock()
    volume.remove.side_effect = docker.errors.APIError("synthetic cleanup failure")
    assert not remove_container(container)
    assert not remove_volume(volume)


def test_missing_daemon_or_image_is_invalid_not_model_failure(tmp_path: Path) -> None:
    client = Mock(spec=docker.DockerClient)
    client.images.get.side_effect = docker.errors.ImageNotFound("missing")
    result = DockerRunner("missing", client=client)(
        tmp_path, "def test_true():\n    assert True\n", seed=11
    )
    assert result.status == "invalid_run"
    assert result.reason == "worker_image_unavailable"
    client.containers.create.assert_not_called()


def test_image_context_cannot_include_project_secrets_or_private_notes(tmp_path: Path) -> None:
    import io
    import tarfile

    from revertbench.sandbox.image import worker_build_context

    package = tmp_path / "src" / "revertbench"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('"""Public code."""\n')
    (tmp_path / "pyproject.toml").write_text("[project]\nname='example'\n")
    (tmp_path / "uv.lock").write_text("version = 1\n")
    (tmp_path / ".env").write_text("SYNTHETIC_SECRET=never-in-image")
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "private.md").write_text("private notes")
    payload = worker_build_context(tmp_path, "python@sha256:" + "a" * 64, b"pytest==9.2.0\n")
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        names = set(archive.getnames())
        dockerfile_member = archive.extractfile("Dockerfile")
        assert dockerfile_member is not None
        dockerfile = dockerfile_member.read()
        data_requirements = archive.extractfile("babel-data-requirements.txt")
        suite_requirements = archive.extractfile("babel-test-requirements.txt")
        assert data_requirements is not None and suite_requirements is not None
        assert data_requirements.read() == (
            b"Babel==2.16.0 --hash=sha256:"
            b"368b5b98b37c06b7daf6696391c3240c938b37767d4584413e8438c5c435fa8b\n"
        )
        assert suite_requirements.read().splitlines() == [
            b"freezegun==1.5.1 --hash=sha256:"
            b"bf111d7138a8abe55ab48a71755673dbaa4ab87f4cff5634a4442dfec34c15f1",
            b"python-dateutil==2.9.0.post0 --hash=sha256:"
            b"a8b2bc7bffae282281c8140a97d3aa9c14da0b136dfe83f850eea9a5f7470427",
        ]
    assert b"COPY src/revertbench /opt/revertbench/src/revertbench" in dockerfile
    assert b"/usr/local/lib/python3.12/site-packages/revertbench.pth" in dockerfile
    assert names == {
        "Dockerfile",
        "requirements.txt",
        "babel-data-requirements.txt",
        "babel-test-requirements.txt",
        "pyproject.toml",
        "uv.lock",
        "src/revertbench/__init__.py",
    }
    assert b"SYNTHETIC_SECRET" not in payload and b"private notes" not in payload
    assert b"--require-hashes" in payload


@pytest.mark.docker
def test_docker_worker_runs_reference_on_bad_and_repair(
    tmp_path: Path,
    docker_client: docker.DockerClient,
    worker_image: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs: list[str] = []
    execute = DockerRunner._run

    def record_job(
        runner: DockerRunner, archive: bytes, job: dict[str, str | int | float], seed: int
    ) -> "ExecutionResult":
        jobs.append(str(job["job_id"]))
        return execute(runner, archive, job, seed)

    monkeypatch.setattr(DockerRunner, "_run", record_job)
    tree = tmp_path / "tree"
    tree.mkdir()
    source = (
        "from project import normalize\ndef test_regression():\n"
        "    assert normalize(' X ') == 'x'\n"
    )
    runner = DockerRunner(worker_image, client=docker_client)
    (tree / "project.py").write_text("def normalize(value):\n    return value.strip()\n")
    bad = runner(tree, source, seed=11)
    assert bad.status == "completed", bad
    assert bad.tests[0].call is not None and bad.tests[0].call.outcome == "failed"
    assert bad.tests[0].covered_lines["project.py"] == (2,)
    (tree / "project.py").write_text("def normalize(value):\n    return value.strip().lower()\n")
    repaired = runner(tree, source, seed=11)
    assert repaired.status == "completed", repaired
    assert repaired.tests[0].call is not None and repaired.tests[0].call.outcome == "passed"
    for job in jobs:
        assert not docker_client.containers.list(
            all=True, filters={"label": f"revertbench.job={job}"}
        )


@pytest.mark.docker
def test_worker_is_offline_nonroot_and_production_is_readonly(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    (tmp_path / "project.py").write_text("VALUE = 'immutable'\n")
    source = """import os
import socket
from pathlib import Path
import pytest

def test_boundaries():
    assert os.getuid() == 10001
    assert 'GROQ_API_KEY' not in os.environ
    assert not Path('/var/run/docker.sock').exists()
    assert not Path('.git').exists()
    assert not Path('/host').exists()
    with pytest.raises(OSError):
        Path('project.py').chmod(0o644)
    with pytest.raises(OSError):
        Path('project.py').write_text('changed')
    with pytest.raises(OSError):
        socket.create_connection(('1.1.1.1', 443), timeout=0.2)
"""
    result = DockerRunner(worker_image, client=docker_client)(tmp_path, source, seed=11)
    assert result.status == "completed", result
    assert result.exit_code == 0, result


@pytest.mark.docker
@pytest.mark.parametrize("kind", ["output", "sleep"])
def test_worker_enforces_output_and_wall_limits(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str, kind: str
) -> None:
    source = "def test_output():\n    print('X' * 100000)\n"
    if kind == "sleep":
        source = "import time\ndef test_sleep():\n    time.sleep(20)\n"
    result = DockerRunner(worker_image, client=docker_client)(
        tmp_path, source, seed=11, timeout_seconds=1.0, output_limit_bytes=1024
    )
    assert result.status == (
        "submission_timeout" if kind == "sleep" else "submission_output_limit"
    ), result
    assert len(result.stdout.encode()) + len(result.stderr.encode()) <= 1024


@pytest.mark.parametrize("ancestor", ["src", "package"])
def test_image_context_rejects_symlinked_source_ancestors(tmp_path: Path, ancestor: str) -> None:
    from revertbench.sandbox.image import worker_build_context
    from revertbench.sandbox.worker import SandboxError

    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='test'\n")
    (project / "uv.lock").write_text("version=1\n")
    if ancestor == "src":
        (outside / "revertbench").mkdir()
        (outside / "revertbench" / "__init__.py").write_text("OUTSIDE = True\n")
        (project / "src").symlink_to(outside, target_is_directory=True)
    else:
        (project / "src").mkdir()
        (outside / "__init__.py").write_text("OUTSIDE = True\n")
        (project / "src" / "revertbench").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SandboxError, match="symlink"):
        worker_build_context(project, "python@sha256:" + "a" * 64, b"pytest==9.2.0\n")


def test_image_build_does_not_inherit_docker_proxy_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import revertbench.sandbox.image as image_module

    monkeypatch.setattr(image_module, "_requirements", lambda root: b"pytest==9.2.0\n")
    client = Mock(spec=docker.DockerClient)
    client.images.pull.return_value.attrs = {"RepoDigests": ["python@sha256:" + "a" * 64]}
    client.images.get.side_effect = docker.errors.ImageNotFound("not cached")
    client.images.build.return_value = (Mock(id="sha256:worker"), [])
    assert image_module.build_worker_image(client) == "sha256:worker"
    assert client.images.build.call_args.kwargs["use_config_proxy"] is False


def test_nested_worker_json_invalidates_run_instead_of_raising() -> None:
    from revertbench.sandbox.worker import _result_from_logs

    result = _result_from_logs(b"[" * 20000 + b"0" + b"]" * 20000, {"job_id": "test"}, 11)
    assert result.status == "invalid_run"


def test_foreign_result_seed_is_a_transport_identity_failure() -> None:
    import json

    from revertbench.sandbox.worker import _result_from_logs
    from revertbench.schema import ExecutionResult

    job: dict[str, str | int | float] = {"job_id": "synthetic", "seed": 11}
    wrong = ExecutionResult(status="completed", seed=37, exit_code=0).model_dump(mode="json")
    data = "\n".join(
        (
            json.dumps({**job, "event": "started"}),
            json.dumps({**job, "event": "finished", "result": wrong}),
        )
    )
    result = _result_from_logs(data.encode(), job, 11)
    assert result.status == "invalid_run"
    assert result.reason == "worker_identity_mismatch"


def test_oom_without_submission_attribution_is_infrastructure_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import revertbench.sandbox.worker as worker_module

    client = Mock(spec=docker.DockerClient)
    volume = Mock()
    volume.name = "synthetic-volume"
    monkeypatch.setattr(worker_module, "seed_tree_volume", lambda *args: volume)
    container = client.containers.create.return_value
    container.id = "synthetic-container"
    container.attrs = {"Image": "sha256:worker", "State": {"OOMKilled": True, "ExitCode": 137}}
    container.labels = {"revertbench.job": "synthetic"}
    runner = DockerRunner("sha256:worker", client=client)
    runner._image_id = "sha256:worker"
    result = runner._run(b"archive", {"job_id": "synthetic", "timeout_seconds": 1}, 11)
    assert result.status == "invalid_run"
    assert result.reason == "worker_oom"
    container.remove.assert_called_once_with(force=True, v=True)
    volume.remove.assert_called_once_with(force=True)


@pytest.mark.docker
def test_worker_repeats_get_fresh_bounded_scratch(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    source = """from pathlib import Path

def test_scratch():
    marker = Path('/tmp/repeat-state')
    assert not marker.exists()
    marker.write_text('created')
"""
    runner = DockerRunner(worker_image, client=docker_client)
    assert runner(tmp_path, source, seed=11).exit_code == 0
    assert runner(tmp_path, source, seed=23).exit_code == 0


@pytest.mark.docker
def test_worker_pid_limit_bounds_controlled_child_pressure(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    source = """import subprocess
import sys

def test_pid_limit():
    children = []
    try:
        for index in range(40):
            try:
                child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'])
            except OSError:
                break
            children.append(child)
        assert 0 < len(children) < 40
    finally:
        for child in children:
            child.kill()
            child.wait()
"""
    result = DockerRunner(worker_image, client=docker_client, limits=SandboxLimits(pids=16))(
        tmp_path, source, seed=11, timeout_seconds=5
    )
    assert result.status == "completed", result
    assert result.exit_code == 0, result


@pytest.mark.docker
def test_worker_oom_never_becomes_a_passing_result(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    source = "def test_memory():\n    blob = bytearray(1024 * 1024 * 1024)\n    assert blob\n"
    result = DockerRunner(
        worker_image, client=docker_client, limits=SandboxLimits(memory_bytes=134217728)
    )(tmp_path, source, seed=11, timeout_seconds=5)
    assert result.status == "invalid_run", result
    assert result.reason in {"worker_oom", "worker_lost"}, result


@pytest.mark.docker
def test_worker_ignores_case_pytest_config_and_conftest(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    (tmp_path / "conftest.py").write_text("raise RuntimeError('unapproved fixture loaded')\n")
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = --not-a-real-option\n")
    result = DockerRunner(worker_image, client=docker_client)(
        tmp_path, "def test_true():\n    assert True\n", seed=11
    )
    assert result.status == "completed", result
    assert result.exit_code == 0, result


def test_partial_start_failure_still_cleans_container_and_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import revertbench.sandbox.worker as worker_module

    client = Mock(spec=docker.DockerClient)
    volume = Mock()
    volume.name = "synthetic-volume"
    monkeypatch.setattr(worker_module, "seed_tree_volume", lambda *args: volume)
    container = client.containers.create.return_value
    container.start.side_effect = docker.errors.APIError("synthetic startup failure")
    runner = DockerRunner("sha256:worker", client=client)
    runner._image_id = "sha256:worker"
    result = runner._run(b"archive", {"job_id": "synthetic", "timeout_seconds": 1}, 11)
    assert result.status == "invalid_run"
    container.remove.assert_called_once_with(force=True, v=True)
    volume.remove.assert_called_once_with(force=True)


def test_cleanup_failure_invalidates_otherwise_complete_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import revertbench.sandbox.worker as worker_module
    from revertbench.schema import ExecutionResult

    client = Mock(spec=docker.DockerClient)
    volume = Mock()
    volume.name = "synthetic-volume"
    monkeypatch.setattr(worker_module, "seed_tree_volume", lambda *args: volume)
    monkeypatch.setattr(worker_module, "_read_logs", lambda container: b"valid synthetic report")
    monkeypatch.setattr(
        worker_module,
        "_result_from_logs",
        lambda data, job, seed: ExecutionResult(status="completed", seed=seed, exit_code=0),
    )
    container = client.containers.create.return_value
    container.id = "synthetic-container"
    container.attrs = {"Image": "sha256:worker", "State": {"OOMKilled": False, "ExitCode": 0}}
    container.labels = {"revertbench.job": "synthetic"}
    container.remove.side_effect = docker.errors.APIError("synthetic cleanup failure")
    runner = DockerRunner("sha256:worker", client=client)
    runner._image_id = "sha256:worker"
    result = runner._run(b"archive", {"job_id": "synthetic", "timeout_seconds": 1}, 11)
    assert result.status == "invalid_run"
    assert result.reason == "worker_cleanup_failed"
    volume.remove.assert_called_once_with(force=True)


@pytest.mark.parametrize(
    "field,value",
    [("memory_bytes", 134217728.0), ("pids", 16.5), ("tmpfs_bytes", 1048576.0), ("cpus", True)],
)
def test_limits_reject_coerced_or_fractional_resource_values(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        SandboxLimits(**{field: value})  # type: ignore[arg-type]


def test_worker_report_cannot_exceed_requested_output_budget() -> None:
    import json

    from revertbench.sandbox.worker import _result_from_logs
    from revertbench.schema import ExecutionResult

    job: dict[str, str | int | float] = {
        "job_id": "synthetic",
        "seed": 11,
        "output_limit_bytes": 10,
    }
    report = ExecutionResult(status="completed", seed=11, exit_code=0, stdout="X" * 11)
    data = "\n".join(
        (
            json.dumps({**job, "event": "started"}),
            json.dumps({**job, "event": "finished", "result": report.model_dump(mode="json")}),
        )
    )
    result = _result_from_logs(data.encode(), job, 11)
    assert result.status == "invalid_run"


@pytest.mark.docker
def test_case_package_precedes_same_named_installed_dependency(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    package = tmp_path / "rich"
    package.mkdir()
    module = package / "__init__.py"
    source = (
        "from rich import normalize\ndef test_regression():\n    assert normalize(' X ') == 'x'\n"
    )
    runner = DockerRunner(worker_image, client=docker_client)
    module.write_text("def normalize(value):\n    return value.strip()\n")
    bad = runner(tmp_path, source, seed=11)
    assert bad.status == "completed", bad
    assert not bad.collection_errors, bad
    assert bad.tests[0].call is not None and bad.tests[0].call.outcome == "failed"
    assert bad.tests[0].covered_lines["rich/__init__.py"] == (2,)
    module.write_text("def normalize(value):\n    return value.strip().lower()\n")
    repaired = runner(tmp_path, source, seed=11)
    assert repaired.status == "completed", repaired
    assert repaired.tests[0].call is not None and repaired.tests[0].call.outcome == "passed"
