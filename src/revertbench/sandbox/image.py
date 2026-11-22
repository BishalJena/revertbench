"""Build the grading image from a strictly public, lockfile-driven context.

Trusted grader source lives apart from general site-packages. A root-owned static
system .pth file permits isolated entrypoint startup; test subprocesses prepend
only that dedicated source directory, then the case's src/root before dependencies.
"""

import hashlib
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import docker

from revertbench.sandbox.worker import SandboxError

_BASE_IMAGE = "python:3.12-slim-bookworm"
_BABEL_DATA_REQUIREMENTS = (
    b"Babel==2.16.0 --hash=sha256:"
    b"368b5b98b37c06b7daf6696391c3240c938b37767d4584413e8438c5c435fa8b\n"
)
_BABEL_CLDR44_DATA_REQUIREMENTS = (
    b"Babel==2.15.0 --hash=sha256:"
    b"08706bdad8d0a3413266ab61bd6c34d0c28d6e1e7badf40a2cebe67644e2e1fb\n"
)
_BABEL_TEST_REQUIREMENTS = (
    b"freezegun==1.5.1 --hash=sha256:"
    b"bf111d7138a8abe55ab48a71755673dbaa4ab87f4cff5634a4442dfec34c15f1\n"
    b"python-dateutil==2.9.0.post0 --hash=sha256:"
    b"a8b2bc7bffae282281c8140a97d3aa9c14da0b136dfe83f850eea9a5f7470427\n"
)
_DOCKERFILE = """FROM {base}
LABEL revertbench.worker.protocol="1"
COPY requirements.txt /opt/revertbench/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /opt/revertbench/requirements.txt
COPY babel-data-requirements.txt babel-test-requirements.txt /opt/revertbench/babel/
RUN python -m pip download --no-cache-dir --no-deps --only-binary=:all: --require-hashes \\
        --dest /tmp/babel-wheel -r /opt/revertbench/babel/babel-data-requirements.txt \\
    && python -I -m zipfile -e /tmp/babel-wheel/babel-2.16.0-py3-none-any.whl /tmp/babel-release \\
    && mkdir -p /opt/revertbench/babel/data/babel/locale-data \\
    && cp /tmp/babel-release/babel/global.dat /opt/revertbench/babel/data/babel/ \\
    && cp /tmp/babel-release/babel/locale-data/*.dat \\
        /opt/revertbench/babel/data/babel/locale-data/ \\
    && rm -rf /tmp/babel-wheel /tmp/babel-release \\
    && python -m pip install --no-cache-dir --no-deps --only-binary=:all: --require-hashes \\
        --target /opt/revertbench/babel/deps -r /opt/revertbench/babel/babel-test-requirements.txt
COPY babel-cldr44-data-requirements.txt /opt/revertbench/babel/
RUN python -m pip download --no-cache-dir --no-deps --only-binary=:all: --require-hashes \\
        --dest /tmp/babel44-wheel -r /opt/revertbench/babel/babel-cldr44-data-requirements.txt \\
    && python -I -m zipfile -e \\
        /tmp/babel44-wheel/Babel-2.15.0-py3-none-any.whl /tmp/babel44-release \\
    && mkdir -p /opt/revertbench/babel/cldr44/data/babel/locale-data \\
    && cp /tmp/babel44-release/babel/global.dat /opt/revertbench/babel/cldr44/data/babel/ \\
    && cp /tmp/babel44-release/babel/locale-data/*.dat \\
        /opt/revertbench/babel/cldr44/data/babel/locale-data/ \\
    && rm -rf /tmp/babel44-wheel /tmp/babel44-release
COPY src/revertbench /opt/revertbench/src/revertbench
RUN printf '/opt/revertbench/src\\n' > /usr/local/lib/python3.12/site-packages/revertbench.pth
COPY pyproject.toml uv.lock /opt/revertbench/
RUN mkdir -p /work/tree && chmod 0555 /work/tree
RUN printf '[pytest]\\naddopts =\\n' > /opt/revertbench/pytest.ini
ENV PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
WORKDIR /work/tree
ENTRYPOINT ["/usr/local/bin/python", "-I", "-m", "revertbench.sandbox._entrypoint"]
"""


def _tar_file(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(content)
    member.mode = 0o444
    member.mtime = 0
    archive.addfile(member, io.BytesIO(content))


def worker_build_context(root: Path, base_image: str, requirements: bytes) -> bytes:
    """Include only package Python source and explicit public dependency artifacts."""
    if "@sha256:" not in base_image or "\n" in base_image:
        raise SandboxError("base_image_must_be_pinned")
    canonical_root = root.resolve()
    package = root / "src" / "revertbench"
    if any(path.is_symlink() for path in (root, root / "src", package)):
        raise SandboxError("build_context_symlink")
    if not package.resolve().is_relative_to(canonical_root):
        raise SandboxError("build_context_symlink")
    if any(path.is_symlink() for path in package.rglob("*")):
        raise SandboxError("build_context_symlink")
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        _tar_file(archive, "Dockerfile", _DOCKERFILE.format(base=base_image).encode())
        _tar_file(archive, "requirements.txt", requirements)
        _tar_file(archive, "babel-data-requirements.txt", _BABEL_DATA_REQUIREMENTS)
        _tar_file(archive, "babel-cldr44-data-requirements.txt", _BABEL_CLDR44_DATA_REQUIREMENTS)
        _tar_file(archive, "babel-test-requirements.txt", _BABEL_TEST_REQUIREMENTS)
        for name in ("pyproject.toml", "uv.lock"):
            path = root / name
            if path.is_symlink():
                raise SandboxError("build_context_symlink")
            _tar_file(archive, name, path.read_bytes())
        package = root / "src" / "revertbench"
        for path in sorted(package.rglob("*.py")):
            relative = path.relative_to(root)
            if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(package.resolve()):
                raise SandboxError("build_context_symlink")
            _tar_file(archive, relative.as_posix(), path.read_bytes())
    return output.getvalue()


def _requirements(root: Path) -> bytes:
    uv = shutil.which("uv")
    if uv is None:
        raise SandboxError("uv_required_to_export_locked_dependencies")
    command = [
        uv,
        "--no-config",
        "export",
        "--frozen",
        "--offline",
        "--no-dev",
        "--no-emit-project",
        "--no-header",
        "--no-annotate",
        "--format",
        "requirements.txt",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env={"PATH": os.defpath},
            capture_output=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SandboxError("locked_dependency_export_failed") from error
    if b"://" in result.stdout or b"--index" in result.stdout or b"-e " in result.stdout:
        raise SandboxError("only_hashed_registry_dependencies_supported")
    return result.stdout


def build_worker_image(client: docker.DockerClient, *, tag: str | None = None) -> str:
    """Build from a digest-pinned Python base and hashed locked dependencies."""
    root = Path(__file__).resolve().parents[3]
    requirements = _requirements(root)
    try:
        base = client.images.pull(_BASE_IMAGE)
        digests = base.attrs.get("RepoDigests", [])
        if not digests:
            raise SandboxError("base_image_digest_unavailable")
        payload = worker_build_context(root, str(digests[0]), requirements)
        chosen_tag = tag or f"revertbench-worker:{hashlib.sha256(payload).hexdigest()[:20]}"
        try:
            cached = client.images.get(chosen_tag)
        except docker.errors.ImageNotFound:
            cached = None
        if (
            cached is not None
            and cached.labels.get("revertbench.worker.context")
            == hashlib.sha256(payload).hexdigest()
        ):
            return str(cached.id)
        image, _ = client.images.build(
            fileobj=io.BytesIO(payload),
            custom_context=True,
            tag=chosen_tag,
            rm=True,
            forcerm=True,
            pull=False,
            timeout=600,
            use_config_proxy=False,
            labels={"revertbench.worker.context": hashlib.sha256(payload).hexdigest()},
        )
        return str(image.id)
    except docker.errors.DockerException as error:
        raise SandboxError("worker_image_build_failed") from error
