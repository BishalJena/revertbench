"""Run the historical attrs field-transformer native suite in its fixed image environment."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import zlib
from collections.abc import Sequence
from importlib import import_module, metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    import pytest

PROFILE: Final = "attrs_field_transformer_default_behavior_v1"
MEMORY_BYTES: Final = 896 * 1024**2
_ROOT = Path("/workspace/repo")
_OVERLAY = Path("/opt/revertbench/attrs")
_RUNTIME = _OVERLAY / "runtime"
_BUILD_PYTHON = _OVERLAY / "build/bin/python"
_RUNTIME_PYTHON = _RUNTIME / "bin/python"
_MODULE = "revertbench._attrs_baseline"
COMMAND = (
    str(_RUNTIME_PYTHON),
    "-m",
    "pytest",
    "-n",
    "1",
    "-c",
    str(_ROOT / "pyproject.toml"),
    "-q",
    "--tb=short",
    "--basetemp=/tmp/revertbench-attrs-pytest",
    "-p",
    "no:cacheprovider",
    "-p",
    "xdist.plugin",
    "-p",
    "xdist.looponfail",
    "-p",
    "_hypothesis_pytestplugin",
    "-p",
    "pytest_mypy_plugins.collect",
    "-p",
    _MODULE,
)
# The unchanged hook file identifies the tag distance for the introducing revision
# and its parent. The production-only repair uses the introducing revision's version.
_VERSIONS = {
    "d727c8d7b3709dc50e16b3304e3ffe646b35661a6f77493461a77d50c84f2645": "25.1.1.dev4",
    "bcea4ddc34749ec3bcbe0837bb0947126236f3c48fc47a6ff3d2e75add14976b": "25.1.1.dev5",
}
# All three wheels were independently built from the exact historical sources, with
# the historical Hatch backend and tag 25.1.0 distances 4 (base) and 5 (bad/repair).
_WHEELS = {
    "c78ec64ca32c04d72e49bc627a98156c909cc5dcb7ebb366ba069259c2b9c375": (
        "25.1.1.dev4",
        "c90da2647835befc582583f215c5753f411fcc6a947c8bdcef68c6fce9753b8a",
    ),
    "024d5006d63dcc412f622d281d558512e7189c85e2e241dce40d57d045adbe7f": (
        "25.1.1.dev5",
        "8371f9b055cb99b220b2e3b5156031314f88ec67b8f2264f0acb2451d504b5f5",
    ),
    "d66f4f8da86013cbe8a536fd07102a4e02ee60266b6957f424c6febc90fcff99": (
        "25.1.1.dev5",
        "8371f9b055cb99b220b2e3b5156031314f88ec67b8f2264f0acb2451d504b5f5",
    ),
}
YAML_PASSED: Final = 85
_SKIPS = {
    **dict.fromkeys(
        (
            f"tests/test_functional.py::TestReplace::test_{name}"
            for name in ("replaces", "already_has_one", "invalid_field_name")
        ),
        "requires Python 3.13+",
    ),
    "tests/test_make.py::TestAutoDetect::test_match_args_pre_310": "Pre-3.10 only.",
    "tests/test_pyright.py::test_pyright_baseline": "Requires pyright.",
    "tests/test_pyright.py::test_pyright_attrsinstance_compat": "Requires pyright.",
    "tests/test_slots.py::test_implicitly_weakrefable": (
        "slots without weakref_slot should only work on PyPy"
    ),
}
_XFAIL = "tests/test_setattr.py::TestSetAttr::test_slotted_confused"


def known_selection(nodes: Sequence[str], wheel_sha256: str, *, skipped: int, xfailed: int) -> bool:
    expected = _WHEELS.get(wheel_sha256)
    digest = hashlib.sha256(json.dumps(list(nodes)).encode()).hexdigest()
    return (
        expected is not None and digest == expected[1] and skipped == len(_SKIPS) and xfailed == 1
    )


def _sha256(path: Path) -> str:
    with path.open("rb") as data:
        return hashlib.file_digest(data, "sha256").hexdigest()


def _report(payload: dict[str, Any]) -> None:
    encoded = base64.b64encode(zlib.compress(json.dumps(payload, separators=(",", ":")).encode()))
    print("\nREVERTBENCH_BASELINE_ZLIB=" + encoded.decode("ascii"), flush=True)


def run_native_attrs(identity: dict[str, str]) -> int:
    """Build the role wheel offline, then run its tests in a separate interpreter."""
    payload: dict[str, Any] = dict(identity, profile=PROFILE, command=COMMAND)
    try:
        with tempfile.TemporaryDirectory(prefix="revertbench-attrs-") as directory:
            scratch = Path(directory)
            wheel_dir, installed = scratch / "wheel", scratch / "site"
            version = _VERSIONS.get(_sha256(_ROOT / "tests/test_hooks.py"))
            if version is None:
                raise ValueError("unknown attrs hook file")
            env = dict(os.environ, PIP_CONFIG_FILE="/dev/null", PYTHONPATH="")
            pip = ["-I", "-m", "pip", "--disable-pip-version-check", "--quiet"]
            subprocess.run(
                [
                    str(_BUILD_PYTHON),
                    *pip,
                    "wheel",
                    "--no-build-isolation",
                    "--no-deps",
                    "--no-index",
                    "--no-cache-dir",
                    "--wheel-dir",
                    str(wheel_dir),
                    str(_ROOT),
                ],
                cwd=_ROOT,
                env=dict(
                    env, SETUPTOOLS_SCM_PRETEND_VERSION=version, SOURCE_DATE_EPOCH="1739084968"
                ),
                check=True,
                timeout=15,
            )
            wheel = wheel_dir / f"attrs-{version}-py3-none-any.whl"
            digest = _sha256(wheel)
            if digest not in _WHEELS or _WHEELS[digest][0] != version:
                raise ValueError("historical attrs wheel digest mismatch")
            subprocess.run(
                [
                    str(_RUNTIME_PYTHON),
                    *pip,
                    "install",
                    "--no-deps",
                    "--no-index",
                    "--no-cache-dir",
                    "--no-compile",
                    "--target",
                    str(installed),
                    str(wheel),
                ],
                env=env,
                check=True,
                timeout=10,
            )
            env.update(
                PATH=f"{_RUNTIME / 'bin'}:{os.defpath}",
                VIRTUAL_ENV=str(_RUNTIME),
                PYTHONPATH=f"/opt/revertbench/src:{installed}",
                PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                PYTEST_ADDOPTS="",
                REVERTBENCH_ATTRS_INSTALLED=str(installed),
                REVERTBENCH_ATTRS_VERSION=version,
            )
            payload["native_attrs_wheel_sha256"] = digest
            launch = f"from {_MODULE} import run_suite; raise SystemExit(run_suite({payload!r}))"
            return subprocess.run(
                [str(_RUNTIME_PYTHON), "-P", "-s", "-c", launch],
                cwd=_ROOT,
                env=env,
                check=False,
            ).returncode
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        desc = f"attrs native preparation failed ({type(error).__name__}): {error}"
        _report(dict(payload, errors=1, details=[desc[:2048]]))
        return 4


def _installed() -> bool:
    attr = import_module("attr")
    attrs = import_module("attrs")

    target = Path(os.environ["REVERTBENCH_ATTRS_INSTALLED"])
    distribution = metadata.distribution("attrs")
    return (
        Path(str(attr.__file__)) == target / "attr/__init__.py"
        and Path(str(attrs.__file__)) == target / "attrs/__init__.py"
        and Path(str(distribution.locate_file(""))) == target
        and distribution.version == os.environ["REVERTBENCH_ATTRS_VERSION"]
    )


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Record the package loaded by the single xdist worker."""
    worker_output = getattr(session.config, "workeroutput", None)
    if isinstance(worker_output, dict):
        worker_output["revertbench_attrs_installed"] = _installed()


class _Evidence:
    def __init__(self) -> None:
        self.nodes: list[str] = []
        self.calls: dict[str, str] = {}
        self.skips: set[str] = set()
        self.xfails: set[str] = set()
        self.xpasses: set[str] = set()
        self.errors: list[str] = []
        self.workers = 0
        self.deselected = 0

    def pytest_xdist_node_collection_finished(self, ids: list[str]) -> None:
        if self.nodes:
            self.errors.append("unexpected extra xdist collection")
        self.nodes = ids

    def pytest_testnodedown(self, node: Any, error: object) -> None:
        self.workers += 1
        if error is not None or node.workeroutput.get("revertbench_attrs_installed") is not True:
            self.errors.append("attrs xdist worker environment mismatch")

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if not report.passed:
            self.errors.append(str(report.longrepr)[:2048])

    def pytest_deselected(self, items: list[pytest.Item]) -> None:
        self.deselected += len(items)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        node = report.nodeid
        expected = hasattr(report, "wasxfail")
        if report.when == "call":
            self.calls[node] = report.outcome
        if expected and report.skipped:
            self.xfails.add(node)
            if node != _XFAIL or report.when != "call":
                self.errors.append(f"unexpected attrs expected failure: {node}")
        elif expected and report.passed:
            self.xpasses.add(node)
        elif report.skipped:
            self.skips.add(node)
            description = report.longrepr
            reason = description[2] if isinstance(description, tuple) else None
            if node not in _SKIPS or report.when != "setup" or reason != f"Skipped: {_SKIPS[node]}":
                self.errors.append(f"unexpected attrs skip: {node}")
        elif report.when != "call" and report.failed:
            self.errors.append(str(report.longrepr)[:2048])


def run_suite(payload: dict[str, Any]) -> int:
    import pytest

    evidence = _Evidence()
    if not _installed():
        evidence.errors.append("attrs controller environment mismatch")
    code = 4 if evidence.errors else int(pytest.main(list(COMMAND[3:]), plugins=[evidence]))
    passed_nodes = {
        node
        for node, outcome in evidence.calls.items()
        if outcome == "passed" and node not in evidence.xpasses
    }
    yaml_passed = sum(node.startswith("tests/test_mypy.yml::") for node in passed_nodes)
    if code == 0 and (
        yaml_passed != YAML_PASSED
        or evidence.workers != 1
        or evidence.skips != _SKIPS.keys()
        or evidence.xfails != {_XFAIL}
    ):
        evidence.errors.append("attrs native typing, skips or xdist execution mismatch")
    _report(
        dict(
            payload,
            collected=len(evidence.nodes),
            node_ids=evidence.nodes,
            passed=len(passed_nodes),
            failed=sum(outcome == "failed" for outcome in evidence.calls.values()),
            skipped=len(evidence.skips),
            xfailed=len(evidence.xfails),
            xpassed=len(evidence.xpasses),
            deselected=evidence.deselected,
            errors=len(evidence.errors),
            details=evidence.errors,
            native_attrs_yaml_passed=yaml_passed,
        )
    )
    return code
