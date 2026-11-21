"""Fresh Docker grading workers with read-only source and explicit lifecycle cleanup."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import docker
import requests
from docker.models.containers import Container
from docker.models.volumes import Volume
from pydantic import ValidationError

from revertbench.schema import ExecutionResult

_TREE_PATH = "/work/tree"
_PROTOCOL = "1"
_REPORT_LIMIT = 2_097_152
_DOCKER_ERRORS = (docker.errors.DockerException, requests.exceptions.RequestException, OSError)


class SandboxError(RuntimeError):
    """A sanitized sandbox lifecycle failure, without credential-bearing SDK messages."""


@dataclass(frozen=True)
class SandboxLimits:
    memory_bytes: int = 268_435_456
    pids: int = 64
    cpus: float = 1.0
    tmpfs_bytes: int = 67_108_864
    uid: int = 10001
    gid: int = 10001

    def __post_init__(self) -> None:
        if any(
            type(value) is not int
            for value in (self.memory_bytes, self.pids, self.tmpfs_bytes, self.uid, self.gid)
        ):
            raise ValueError("memory, PID, tmpfs and identity limits must be strict integers")
        if type(self.cpus) not in (int, float):
            raise ValueError("CPU limit must be a number, not a coerced value")
        if not 67_108_864 <= self.memory_bytes <= 2_147_483_648:
            raise ValueError("memory must be between 64 MiB and 2 GiB")
        if not 8 <= self.pids <= 256:
            raise ValueError("PID limit must be between 8 and 256")
        if not math.isfinite(self.cpus) or not 0.1 <= self.cpus <= 8:
            raise ValueError("CPU limit must be finite and between 0.1 and 8")
        if not 1_048_576 <= self.tmpfs_bytes <= 268_435_456:
            raise ValueError("tmpfs must be between 1 MiB and 256 MiB")
        if self.uid != 10001 or self.gid != 10001:
            raise ValueError("worker identity must be the fixed unprivileged UID/GID")


def hardened_container_options(limits: SandboxLimits) -> dict[str, Any]:
    """Shared fixed isolation options; callers add only managed volumes and bounded tmpfs."""
    scratch = (
        f"rw,noexec,nosuid,nodev,size={limits.tmpfs_bytes},"
        f"uid={limits.uid},gid={limits.gid},mode=700"
    )
    return {
        "user": f"{limits.uid}:{limits.gid}",
        "read_only": True,
        "network_disabled": True,
        "network_mode": "none",
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "pids_limit": limits.pids,
        "mem_limit": limits.memory_bytes,
        "memswap_limit": limits.memory_bytes,
        "nano_cpus": int(limits.cpus * 1_000_000_000),
        "init": True,
        "tmpfs": {"/run": scratch, "/tmp": scratch},
        "shm_size": 8_388_608,
        "hostname": "revertbench",
        "use_config_proxy": False,
        "log_config": docker.types.LogConfig(
            type="json-file", config={"max-size": "2m", "max-file": "1"}
        ),
        "ulimits": [
            docker.types.Ulimit(name="core", soft=0, hard=0),
            docker.types.Ulimit(name="nofile", soft=128, hard=128),
        ],
    }


def remove_container(container: Container) -> bool:
    try:
        container.remove(force=True, v=True)
    except docker.errors.NotFound:
        return True
    except _DOCKER_ERRORS:
        return False
    return True


def remove_volume(volume: Volume) -> bool:
    try:
        volume.remove(force=True)
    except docker.errors.NotFound:
        return True
    except _DOCKER_ERRORS:
        return False
    return True


def seed_tree_volume(
    client: docker.DockerClient, image: str, archive: bytes, limits: SandboxLimits
) -> Volume:
    """Seed a bounded canonical archive without starting the temporary helper."""
    volume: Volume | None = None
    helper: Container | None = None
    succeeded = False
    try:
        volume = client.volumes.create(
            name=f"revertbench-source-{uuid.uuid4().hex}", labels={"revertbench.managed": "true"}
        )
        helper = client.containers.create(
            image,
            entrypoint=["/bin/true"],
            command=[],
            volumes={volume.name: {"bind": _TREE_PATH, "mode": "rw"}},
            labels={"revertbench.managed": "true", "revertbench.role": "seed"},
            **hardened_container_options(limits),
        )
        if not helper.put_archive(_TREE_PATH, archive):
            raise SandboxError("archive_seed_failed")
        if not remove_container(helper):
            raise SandboxError("helper_cleanup_failed")
        helper = None
        succeeded = True
        return volume
    except _DOCKER_ERRORS as error:
        raise SandboxError("archive_seed_failed") from error
    finally:
        if helper is not None:
            remove_container(helper)
        if not succeeded and volume is not None:
            remove_volume(volume)


def _invalid(seed: int, reason: str) -> ExecutionResult:
    return ExecutionResult(status="invalid_run", seed=seed, reason=reason)


def _read_logs(container: Container) -> bytes:
    stream = container.logs(stdout=True, stderr=True, stream=True, follow=False)
    chunks = bytearray()
    try:
        for chunk in stream:
            chunks.extend(chunk)
            if len(chunks) > _REPORT_LIMIT:
                raise SandboxError("worker_report_limit")
    finally:
        stream.close()
    return bytes(chunks)


def _result_from_logs(data: bytes, job: dict[str, str | int | float], seed: int) -> ExecutionResult:
    try:
        packets = [json.loads(line) for line in data.splitlines() if line.strip()]
        if len(packets) != 2:
            return _invalid(seed, "worker_report_invalid")
        start, end = packets
        for packet in packets:
            if not isinstance(packet, dict) or any(
                packet.get(key) != value for key, value in job.items()
            ):
                return _invalid(seed, "worker_identity_mismatch")
        if start.get("event") != "started" or end.get("event") != "finished":
            return _invalid(seed, "worker_report_invalid")
        result = ExecutionResult.model_validate_json(json.dumps(end["result"]))
        if result.seed != seed:
            return _invalid(seed, "worker_identity_mismatch")
        if len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8")) > int(
            job.get("output_limit_bytes", 65536)
        ):
            return _invalid(seed, "worker_report_invalid")
        return result
    except (ValueError, KeyError, TypeError, ValidationError, RecursionError, UnicodeError):
        return _invalid(seed, "worker_report_invalid")


class DockerRunner:
    """ExecutionBackend using one immutable worker image and a fresh source volume per run."""

    def __init__(
        self,
        image: str,
        *,
        client: docker.DockerClient | None = None,
        limits: SandboxLimits | None = None,
    ) -> None:
        self.image = image
        self.client = client
        self.limits = limits or SandboxLimits()
        self._image_id: str | None = None

    def __call__(
        self,
        tree: Path,
        submission: str,
        *,
        seed: int,
        timeout_seconds: float = 10,
        output_limit_bytes: int = 65536,
    ) -> ExecutionResult:
        return self.run_tree(
            tree,
            submission,
            seed=seed,
            timeout_seconds=timeout_seconds,
            output_limit_bytes=output_limit_bytes,
        )

    def run_tree(
        self,
        tree: Path,
        submission: str,
        *,
        seed: int,
        timeout_seconds: float = 10,
        output_limit_bytes: int = 65536,
    ) -> ExecutionResult:
        if (
            not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or type(seed) is not int
            or not 0 <= seed <= 4_294_967_295
            or type(output_limit_bytes) is not int
            or not 1 <= output_limit_bytes <= 65_536
        ):
            return _invalid(seed, "invalid_limits")
        try:
            self.client = self.client or docker.from_env(timeout=10)
            if self._image_id is None:
                image = self.client.images.get(self.image)
                if image.labels.get("revertbench.worker.protocol") != _PROTOCOL:
                    return _invalid(seed, "worker_image_untrusted")
                self._image_id = image.id
        except _DOCKER_ERRORS:
            return _invalid(seed, "worker_image_unavailable")
        from revertbench.sandbox.archives import ArchiveError, archive_tree, single_file_archive

        try:
            archive = archive_tree(tree)
            # Docker accepts concatenated member streams only after their zero trailers are removed.
            tree_payload = _combine_archives(
                archive, single_file_archive(submission.encode("utf-8"))
            )
        except (ArchiveError, OSError, ValueError, UnicodeError):
            return _invalid(seed, "invalid_case_archive")
        job: dict[str, str | int | float] = {
            "protocol": _PROTOCOL,
            "job_id": uuid.uuid4().hex,
            "tree_sha256": hashlib.sha256(archive).hexdigest(),
            "submission_sha256": hashlib.sha256(submission.encode("utf-8")).hexdigest(),
            "seed": seed,
            "timeout_seconds": timeout_seconds,
            "output_limit_bytes": output_limit_bytes,
        }
        return self._run(tree_payload, job, seed)

    def _run(self, archive: bytes, job: dict[str, str | int | float], seed: int) -> ExecutionResult:
        assert self.client is not None and self._image_id is not None
        volume: Volume | None = None
        container: Container | None = None
        result = _invalid(seed, "worker_start_failed")
        try:
            volume = seed_tree_volume(self.client, self._image_id, archive, self.limits)
            container = self.client.containers.create(
                self._image_id,
                volumes={volume.name: {"bind": _TREE_PATH, "mode": "ro"}},
                environment={"REVERTBENCH_JOB": json.dumps(job)},
                labels={"revertbench.managed": "true", "revertbench.job": str(job["job_id"])},
                **hardened_container_options(self.limits),
            )
            expected_id = container.id
            container.start()
            try:
                container.wait(timeout=float(job["timeout_seconds"]) + 3)
            except requests.exceptions.ReadTimeout:
                container.kill()
                result = _invalid(seed, "worker_wall_timeout")
            else:
                container.reload()
                if (
                    container.id != expected_id
                    or container.attrs.get("Image") != self._image_id
                    or container.labels.get("revertbench.job") != job["job_id"]
                ):
                    result = _invalid(seed, "worker_identity_mismatch")
                elif container.attrs.get("State", {}).get("OOMKilled"):
                    # Kernel OOM evidence proves a limit was hit, not whether pytest startup
                    # or the submitted call caused it. Keep ambiguous worker loss unscored.
                    result = _invalid(seed, "worker_oom")
                elif container.attrs.get("State", {}).get("ExitCode") != 0:
                    result = _invalid(seed, "worker_lost")
                else:
                    result = _result_from_logs(_read_logs(container), job, seed)
        except (SandboxError, *_DOCKER_ERRORS):
            result = _invalid(seed, "worker_transport_failed")
        finally:
            cleaned = container is None or remove_container(container)
            cleaned = (volume is None or remove_volume(volume)) and cleaned
            if not cleaned:
                result = _invalid(seed, "worker_cleanup_failed")
        return result


def _combine_archives(first: bytes, second: bytes) -> bytes:
    import io
    import tarfile

    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as target:
        for payload in (first, second):
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as source:
                for member in source:
                    target.addfile(member, source.extractfile(member) if member.isfile() else None)
    return output.getvalue()
