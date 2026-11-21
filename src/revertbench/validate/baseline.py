"""Run existing repository suites only inside isolated containers."""

from __future__ import annotations

import base64
import binascii
import json
import zlib
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated, Literal, Protocol

import docker
import requests
from pydantic import Field, ValidationError

from revertbench.grade.backend import ExecutionBackend
from revertbench.grade.policy import check_submission
from revertbench.grade.rules import _eligible_failure, report_problem
from revertbench.sandbox.agent import AgentSandbox
from revertbench.sandbox.worker import DockerRunner, SandboxError, SandboxLimits
from revertbench.schema import ContentDigest, EvidenceModel

_COUNT = Annotated[int, Field(ge=0)]
BaselinePidsLimit = Annotated[int, Field(ge=8, le=256)]
BaselineProfile = Literal["pytest", "unittest"]
_PREFIX = b"REVERTBENCH_BASELINE="
_ZLIB_PREFIX = b"REVERTBENCH_BASELINE_ZLIB="
_MAX_REPORT_BYTES = 2_097_152
_PYTEST_ARGS = (
    "-q",
    "--disable-warnings",
    "-c",
    "/opt/revertbench/pytest.ini",
    "--rootdir",
    "/workspace/repo",
    "--basetemp=/tmp/revertbench-baseline",
    "-p",
    "no:cacheprovider",
    "tests",
)


def baseline_command(profile: BaselineProfile) -> tuple[str, ...]:
    """Return the fixed effective test command; a trusted reporting shim runs it."""
    if profile == "pytest":
        return ("/usr/local/bin/python", "-m", "pytest", *_PYTEST_ARGS)
    if profile == "unittest":
        return ("/usr/local/bin/python", "-m", "unittest", "-q")
    raise ValueError("baseline profile must be pytest or unittest")


# This trusted controller is executed by isolated Python inside the container.
# Repository imports begin only after pytest and the reporting hook are loaded.
_ENVIRONMENT_SCRIPT = r"""
import hashlib
import json
import sys
from pathlib import Path
python_version = '.'.join(map(str, sys.version_info[:3]))
lock_digest = hashlib.sha256(Path('/opt/revertbench/uv.lock').read_bytes()).hexdigest()
trusted_source = Path('/opt/revertbench/src/revertbench')
grader_hasher = hashlib.sha256()
for path in sorted(trusted_source.rglob('*.py')):
    name, data = path.relative_to(trusted_source).as_posix().encode(), path.read_bytes()
    grader_hasher.update(len(name).to_bytes(8, 'big') + name)
    grader_hasher.update(len(data).to_bytes(8, 'big') + data)
grader_digest = grader_hasher.hexdigest()
policy_digest = hashlib.sha256((trusted_source / 'grade/policy.py').read_bytes()).hexdigest()

"""

_BASELINE_SCRIPT = (
    _ENVIRONMENT_SCRIPT
    + r"""
import pytest
root = '/workspace/repo'
class Evidence:
    def __init__(self):
        self.nodes = []
        self.calls = {}
        self.skips = set()
        self.xfails = set()
        self.xpasses = set()
        self.errors = []
        self.deselected = 0
    def pytest_collection_finish(self, session):
        self.nodes = [item.nodeid for item in session.items]
    def pytest_deselected(self, items):
        self.deselected += len(items)
    def pytest_collectreport(self, report):
        if report.failed:
            self.errors.append(str(report.longrepr)[:2048])
    def pytest_runtest_logreport(self, report):
        node = report.nodeid
        expected = hasattr(report, 'wasxfail')
        if report.when == 'call':
            self.calls[node] = report.outcome
        if expected and report.outcome == 'skipped':
            self.xfails.add(node)
        elif expected and report.outcome == 'passed':
            self.xpasses.add(node)
        elif report.outcome == 'skipped':
            self.skips.add(node)
        elif report.when != 'call' and report.outcome == 'failed':
            self.errors.append(str(report.longrepr)[:2048])

plugin = Evidence()
sys.path[:0] = [root + '/src', root]
command = ['/usr/local/bin/python', '-m', 'pytest',
    '-q', '--disable-warnings', '-c', '/opt/revertbench/pytest.ini',
    '--rootdir', root, '--basetemp=/tmp/revertbench-baseline',
    '-p', 'no:cacheprovider', 'tests',
]
code = int(pytest.main(command[3:], plugins=[plugin]))
passed = sum(value == 'passed' and node not in plugin.xpasses
    for node, value in plugin.calls.items())
failed = sum(value == 'failed' for value in plugin.calls.values())
payload = dict(profile='pytest', command=command, collected=len(plugin.nodes),
    passed=passed, failed=failed,
    skipped=len(plugin.skips), xfailed=len(plugin.xfails), xpassed=len(plugin.xpasses),
    errors=len(plugin.errors), details=plugin.errors, node_ids=plugin.nodes,
    deselected=plugin.deselected, environment_python=python_version,
    dependency_lock_sha256=lock_digest, grader_sha256=grader_digest, policy_sha256=policy_digest)
print('\nREVERTBENCH_BASELINE=' + json.dumps(payload, separators=(',', ':')), flush=True)
sys.exit(code)
"""
)


_UNITTEST_SCRIPT = (
    _ENVIRONMENT_SCRIPT
    + r"""
import base64
import unittest
import zlib

class Evidence(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.nodes = []
        self.success_count = 0
        self.success_nodes = []
        self.skip_events = []
        self.current_node = None
    def startTest(self, test):
        self.current_node = test.id()
        self.nodes.append(test.id())
        super().startTest(test)
    def stopTest(self, test):
        super().stopTest(test)
        self.current_node = None
    def addSuccess(self, test):
        self.success_count += 1
        self.success_nodes.append(test.id())
        super().addSuccess(test)
    def addSkip(self, test, reason):
        self.skip_events.append((test.id(), self.current_node))
        super().addSkip(test, reason)

class Runner(unittest.TextTestRunner):
    resultclass = Evidence

root = '/workspace/repo'
sys.path[:0] = [root + '/src', root]
loader = unittest.TestLoader()
program = unittest.main(module=None, argv=['unittest', '-q'],
    testLoader=loader, testRunner=Runner, exit=False)
result = program.result
command = ['/usr/local/bin/python', '-m', 'unittest', '-q']
details = [test.id() + ': ' + text[:2048] for test, text in result.errors + result.failures]
remaining = set(result.nodes) - set(result.success_nodes)
remaining -= {test.id() for test, _ in result.expectedFailures}
skip_parents = set()
extra_skips = []
for event_id, parent_id in result.skip_events:
    if parent_id in remaining and parent_id not in skip_parents:
        skip_parents.add(parent_id)
    else:
        extra_skips.append(event_id)
payload = dict(profile='unittest', command=command, collected=result.testsRun,
    passed=result.success_count, failed=len(result.failures), errors=len(result.errors),
    skipped=len(result.skipped), xfailed=len(result.expectedFailures),
    xpassed=len(result.unexpectedSuccesses), node_ids=result.nodes, details=details,
    extra_skip_node_ids=extra_skips,
    collection_errors=[text[:2048] for text in loader.errors], environment_python=python_version,
    dependency_lock_sha256=lock_digest, grader_sha256=grader_digest, policy_sha256=policy_digest)
encoded = base64.b64encode(zlib.compress(json.dumps(payload, separators=(',', ':')).encode()))
print('\nREVERTBENCH_BASELINE_ZLIB=' + encoded.decode('ascii'), flush=True)
sys.exit(0 if result.wasSuccessful() else 1)
"""
)


class BaselineResult(EvidenceModel):
    status: Literal["passed", "failed", "invalid_run"]
    profile: BaselineProfile = "pytest"
    command: tuple[str, ...] = ()
    pids_limit: BaselinePidsLimit = 64
    collection_errors: tuple[str, ...] = ()
    extra_skip_node_ids: tuple[str, ...] = ()
    environment_python: str | None = None
    dependency_lock_sha256: ContentDigest | None = None
    grader_sha256: ContentDigest | None = None
    policy_sha256: ContentDigest | None = None
    reason: str | None = None
    exit_code: int | None = None
    collected: _COUNT = 0
    passed: _COUNT = 0
    failed: _COUNT = 0
    errors: _COUNT = 0
    skipped: _COUNT = 0
    xfailed: _COUNT = 0
    xpassed: _COUNT = 0
    deselected: _COUNT = 0
    node_ids: tuple[str, ...] = ()
    details: tuple[str, ...] = ()
    stdout: str = ""
    stderr: str = ""


class BaselineBackend(Protocol):
    def __call__(self, tree: Path) -> BaselineResult: ...


def _report_payload(stdout: bytes) -> object:
    plain = [line[len(_PREFIX) :] for line in stdout.splitlines() if line.startswith(_PREFIX)]
    packed = [
        line[len(_ZLIB_PREFIX) :] for line in stdout.splitlines() if line.startswith(_ZLIB_PREFIX)
    ]
    if len(plain) + len(packed) != 1:
        raise ValueError("missing or duplicate baseline report")
    data = plain[0] if plain else base64.b64decode(packed[0], validate=True)
    if packed:
        decoder = zlib.decompressobj()
        data = decoder.decompress(data, _MAX_REPORT_BYTES + 1)
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError("incomplete or oversized baseline report")
    if len(data) > _MAX_REPORT_BYTES:
        raise ValueError("oversized baseline report")
    return json.loads(data)


def parse_baseline_output(
    stdout: bytes,
    stderr: bytes,
    exit_code: int | None,
    *,
    expected_profile: BaselineProfile = "pytest",
    expected_pids_limit: int = 64,
) -> BaselineResult:
    SandboxLimits(pids=expected_pids_limit)
    common = {
        "pids_limit": expected_pids_limit,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "exit_code": exit_code,
    }
    try:
        payload = _report_payload(stdout)
        if not isinstance(payload, dict):
            raise ValueError("baseline report must be an object")
        result = BaselineResult.model_validate_json(
            json.dumps({**payload, **common, "status": "invalid_run"})
        )
    except (ValueError, TypeError, ValidationError, RecursionError, binascii.Error, zlib.error):
        return BaselineResult.model_validate(
            {"status": "invalid_run", "reason": "baseline_report_invalid", **common}
        )
    if result.profile != expected_profile:
        return result.model_copy(update={"reason": "baseline_profile_mismatch"})
    if result.errors and any("SyntaxError:" in detail for detail in result.details):
        return result.model_copy(update={"status": "failed", "reason": "unsupported_environment"})
    if (
        result.collection_errors
        or (result.profile == "pytest" and result.errors)
        or exit_code in (2, 3, 4)
        or exit_code not in (0, 1, 5)
    ):
        return result.model_copy(update={"reason": "baseline_collection_or_worker_error"})
    counted = result.passed + result.failed + result.skipped + result.xfailed + result.xpassed
    if result.profile == "unittest":
        counted += result.errors
    inconsistent = (
        len(result.node_ids) != result.collected
        or len(set(result.node_ids)) != result.collected
        or result.passed > result.collected
        or len(result.extra_skip_node_ids) > result.skipped
        or any(not node or node in result.node_ids for node in result.extra_skip_node_ids)
        or (exit_code == 0 and result.failed)
    )
    if result.profile == "pytest":
        inconsistent |= counted != result.collected or bool(result.extra_skip_node_ids)
    elif not (result.failed or result.errors or result.xpassed):
        inconsistent |= counted - len(result.extra_skip_node_ids) != result.collected
    if inconsistent:
        return result.model_copy(update={"reason": "baseline_report_inconsistent"})
    if result.profile == "unittest" and (result.failed or result.errors or result.xpassed):
        return result.model_copy(update={"status": "failed", "reason": "existing_suite_failed"})
    if result.collected == 0 or result.passed == 0:
        return result.model_copy(update={"status": "failed", "reason": "no_executed_tests"})
    if result.failed or exit_code == 1:
        return result.model_copy(update={"status": "failed", "reason": "existing_suite_failed"})
    if exit_code != 0:
        return result.model_copy(update={"reason": "baseline_report_inconsistent"})
    if result.deselected:
        return result.model_copy(update={"status": "failed", "reason": "suite_deselected_tests"})
    return result.model_copy(update={"status": "passed"})


class DockerBaselineRunner:
    """Run each existing suite in a fresh bounded agent workspace with original fixtures."""

    def __init__(
        self,
        image: str,
        *,
        client: docker.DockerClient | None = None,
        limits: SandboxLimits | None = None,
        profile: BaselineProfile = "pytest",
    ) -> None:
        baseline_command(profile)
        self.profile = profile
        self.image, self.client = image, client
        self.limits = limits or SandboxLimits()

    def __call__(self, tree: Path) -> BaselineResult:
        try:
            with ExitStack() as resources:
                client = self.client
                if client is None:
                    client = docker.from_env(timeout=70)
                    resources.callback(client.close)
                with AgentSandbox(self.image, tree, client=client, limits=self.limits) as box:
                    execution = box.exec(
                        [
                            "/usr/local/bin/python",
                            "-I",
                            "-c",
                            _BASELINE_SCRIPT if self.profile == "pytest" else _UNITTEST_SCRIPT,
                        ],
                        timeout_seconds=60,
                        output_limit_bytes=65_536,
                    )
            if execution.status != "completed":
                return BaselineResult(
                    status="invalid_run",
                    profile=self.profile,
                    pids_limit=self.limits.pids,
                    reason=f"baseline_{execution.status}",
                )
            return parse_baseline_output(
                execution.stdout,
                execution.stderr,
                execution.exit_code,
                expected_profile=self.profile,
                expected_pids_limit=self.limits.pids,
            )
        except (
            SandboxError,
            docker.errors.DockerException,
            requests.exceptions.RequestException,
            OSError,
        ) as error:
            return BaselineResult(
                status="invalid_run",
                profile=self.profile,
                pids_limit=self.limits.pids,
                reason="baseline_sandbox_unavailable",
                details=(type(error).__name__,),
            )


class DockerReferenceProbe:
    """Search predicate using grader exception rules and each failed node's project evidence.

    Search has not established introducing lines yet. Final case validation still
    requires exact introducing-line coverage and three stable repeats.
    """

    def __init__(
        self,
        image: str,
        *,
        client: docker.DockerClient | None = None,
        execution_backend: ExecutionBackend | None = None,
    ) -> None:
        self.execute = execution_backend or DockerRunner(image, client=client)

    def classify(self, tree: Path, source: str) -> Literal["pass", "fail", "inconclusive"]:
        policy = check_submission(source, tree)
        if not policy.allowed:
            return "inconclusive"
        result = self.execute(tree, source, seed=11, timeout_seconds=10, output_limit_bytes=65_536)
        if result.status != "completed" or report_problem(result):
            return "inconclusive"
        failures = [test for test in result.tests if test.call and test.call.outcome == "failed"]
        if not failures:
            return "pass"
        if all(
            test.call and _eligible_failure(test, test.covered_lines) and test.covered_lines
            for test in failures
        ):
            return "fail"
        return "inconclusive"
