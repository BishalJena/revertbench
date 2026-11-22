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
# attrs default py312-tests wheels, published by the introducing revision date.
_ATTRS_BUILD_REQUIREMENTS = (
    b"hatch-fancy-pypi-readme==24.1.0"
    b" --hash=sha256:26ec5c7cfd9f604eff0ae6c927d7b197b220706dca203f0aad1928abc81f3a46\n"
    b"hatch-vcs==0.4.0"
    b" --hash=sha256:b8a2b6bee54cf6f9fc93762db73890017ae59c9081d1038a41f16235ceaf8b2c\n"
    b"hatchling==1.27.0"
    b" --hash=sha256:d3a2f3567c4f926ea39849cdf924c7e99e6686c9c8e288ae1037c8fa2a5d937b\n"
    b"packaging==24.2"
    b" --hash=sha256:09abb1bccd265c01f4a3aa3f7a7db064b36514d2cba19a2f694fe6150451a759\n"
    b"pathspec==0.12.1"
    b" --hash=sha256:a0d503e138a4c123b27490a4f7beda6a01c6f288df0e4a8b79c7eb0dc7b4cc08\n"
    b"pluggy==1.5.0"
    b" --hash=sha256:44e1ad92c8ca002de6377e165f3e0f1be63266ab4d554740532335b9d75ea669\n"
    b"setuptools==75.8.0"
    b" --hash=sha256:e3982f444617239225d675215d51f6ba05f845d4eec313da4418fdbb56fb27e3\n"
    b"setuptools-scm==8.1.0"
    b" --hash=sha256:897a3226a6fd4a6eb2f068745e49733261a21f70b1bb28fce0339feb978d9af3\n"
    b"trove-classifiers==2025.1.15.22"
    b" --hash=sha256:5f19c789d4f17f501d36c94dbbf969fb3e8c2784d008e6f5164dd2c3d6a2b07c\n"
)
_ATTRS_RUNTIME_REQUIREMENTS = (
    b"cloudpickle==3.0.0"
    b" --hash=sha256:246ee7d0c295602a036e86369c77fecda4ab17b506496730f2f576d9016fd9c7\n"
    b"decorator==5.1.1"
    b" --hash=sha256:b8c3f85900b9dc423225913c5aace94729fe1fa9763b38939a95226f02d37186\n"
    b"execnet==2.1.1"
    b" --hash=sha256:26dee51f1b80cebd6d0ca8e74dd8745419761d3bef34163928cbebbdc4749fdc\n"
    b"hypothesis==6.103.1"
    b" --hash=sha256:d3c959fab6233e78867499e2117ae9db8dc40eeed936d71a2cfc7b6094972e74\n"
    b"iniconfig==2.0.0"
    b" --hash=sha256:b6a85871a79d2e3b22d2d1b94ac2824226a63c6b741c88f7ae975f18b6778374\n"
    b"jinja2==3.1.5"
    b" --hash=sha256:aba0f4dc9ed8013c424088f68a5c226f7d6097ed89b246d7749c2ec4175c6adb\n"
    b"jsonschema==4.23.0"
    b" --hash=sha256:fbadb6f8b144a8f8cf9f0b89ba94501d143e50411a1278633f56a7acf7fd5566\n"
    b"jsonschema-specifications==2024.10.1"
    b" --hash=sha256:a09a0680616357d9a0ecf05c12ad234479f549239d0f5b55f3deea67475da9bf\n"
    b"markupsafe==3.0.2"
    b" --hash=sha256:1c99d261bd2d5f6b59325c92c73df481e05e57f19837bdca8413b9eac4bd8028"
    b" --hash=sha256:e17c96c14e19278594aa4841ec148115f9c7615a47382ecb6b82bd8fea3ab0c8\n"
    b"mypy==1.11.1"
    b" --hash=sha256:0624bdb940255d2dd24e829d99a13cfeb72e4e9031f9492148f410ed30bcab54\n"
    b"mypy-extensions==1.0.0"
    b" --hash=sha256:4392f6c0eb8a5668a69e23d168ffa70f0be9ccfd32b5cc2d26a34ae5b844552d\n"
    b"packaging==24.2"
    b" --hash=sha256:09abb1bccd265c01f4a3aa3f7a7db064b36514d2cba19a2f694fe6150451a759\n"
    b"pluggy==1.5.0"
    b" --hash=sha256:44e1ad92c8ca002de6377e165f3e0f1be63266ab4d554740532335b9d75ea669\n"
    b"psutil==6.0.0"
    b" --hash=sha256:5fd9a97c8e94059b0ef54a7d4baf13b405011176c3b6ff257c247cae0d560ecd"
    b" --hash=sha256:e2e8d0054fc88153ca0544f5c4d554d42e33df2e009c4ff42284ac9ebdef4132\n"
    b"pympler==1.1"
    b" --hash=sha256:5b223d6027d0619584116a0cbc28e8d2e378f7a79c1e5e024f9ff3b673c58506\n"
    b"pytest==8.3.4"
    b" --hash=sha256:50e16d954148559c9a74109af1eaf0c945ba2d8f30f0a3d3335edde19788b6f6\n"
    b"pytest-mypy-plugins==3.1.2"
    b" --hash=sha256:3478ccb68e26a159e1287c4614c60f84cd0720e3895f174365c7440498234b9f\n"
    b"pytest-xdist==3.6.1"
    b" --hash=sha256:9ed4adfb68a016610848639bb7e02c9352d5d9f03d04809919e2dafc3be4cca7\n"
    b"pyyaml==6.0.2"
    b" --hash=sha256:1f71ea527786de97d1a0cc0eacd1defc0985dcf6b3f17bb77dcfc8c34bec4dc5"
    b" --hash=sha256:80bab7bfc629882493af4aa31a4cfa43a4c57c83813253626916b8c7ada83476\n"
    b"referencing==0.36.2"
    b" --hash=sha256:e8699adbbf8b5c7de96d8ffa0eb5c158b3beafce084968e2ea8bb08c6794dcd0\n"
    b"regex==2024.11.6"
    b" --hash=sha256:70b7fa6606c2881c1db9479b0eaa11ed5dfa11c8d60a474ff0e095099f39d98e"
    b" --hash=sha256:b85c2530be953a890eaffde05485238f07029600e8f098cdf1848d414a8b45e4\n"
    b"rpds-py==0.22.3"
    b" --hash=sha256:655ca44a831ecb238d124e0402d98f6212ac527a0ba6c55ca26f616604e60a45"
    b" --hash=sha256:e67ba3c290821343c192f7eae1d8fd5999ca2dc99994114643e2f2d3e6138b15\n"
    b"sortedcontainers==2.4.0"
    b" --hash=sha256:a163dcaede0f1c021485e957a39245190e74249897e2ae4b2aa38595db237ee0\n"
    b"tomlkit==0.13.2"
    b" --hash=sha256:7a974427f6e119197f670fbbbeae7bef749a6c14e793db934baefc1b5f03efde\n"
    b"typing-extensions==4.12.2"
    b" --hash=sha256:04e5ca0351e0f3f85c6853954072df659d0d13fac324d0072316b67d7794700d\n"
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
COPY attrs-build.txt attrs-runtime.txt /opt/revertbench/attrs/
RUN python -m venv /opt/revertbench/attrs/build \\
    && /opt/revertbench/attrs/build/bin/python -m pip install \\
        --no-cache-dir --no-deps --only-binary=:all: --require-hashes \\
        -r /opt/revertbench/attrs/attrs-build.txt \\
    && python -m venv /opt/revertbench/attrs/runtime \\
    && /opt/revertbench/attrs/runtime/bin/python -m pip install \\
        --no-cache-dir --no-deps --only-binary=:all: --require-hashes \\
        -r /opt/revertbench/attrs/attrs-runtime.txt
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
        _tar_file(archive, "attrs-build.txt", _ATTRS_BUILD_REQUIREMENTS)
        _tar_file(archive, "attrs-runtime.txt", _ATTRS_RUNTIME_REQUIREMENTS)
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
