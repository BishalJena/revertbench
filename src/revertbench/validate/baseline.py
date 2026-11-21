"""Run existing repository suites only inside isolated containers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal, Protocol

import docker
from pydantic import Field, ValidationError

from revertbench.grade.backend import ExecutionBackend
from revertbench.grade.policy import check_submission
from revertbench.grade.rules import _eligible_failure, report_problem
from revertbench.sandbox.agent import AgentSandbox
from revertbench.sandbox.worker import DockerRunner, SandboxError, SandboxLimits
from revertbench.schema import ContentDigest, EvidenceModel

_COUNT = Annotated[int, Field(ge=0)]
_PREFIX = b"REVERTBENCH_BASELINE="
# This trusted controller is executed by isolated Python inside the container.
# Repository imports begin only after pytest and the reporting hook are loaded.
_BASELINE_SCRIPT = r"""
import hashlib
import json
import sys
from pathlib import Path
import pytest

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
code = int(pytest.main([
    '-q', '--disable-warnings', '-c', '/opt/revertbench/pytest.ini',
    '--rootdir', root, '--basetemp=/tmp/revertbench-baseline',
    '-p', 'no:cacheprovider', 'tests',
], plugins=[plugin]))
passed = sum(value == 'passed' and node not in plugin.xpasses
    for node, value in plugin.calls.items())
failed = sum(value == 'failed' for value in plugin.calls.values())
payload = dict(collected=len(plugin.nodes), passed=passed, failed=failed,
    skipped=len(plugin.skips), xfailed=len(plugin.xfails), xpassed=len(plugin.xpasses),
    errors=len(plugin.errors), details=plugin.errors, node_ids=plugin.nodes,
    deselected=plugin.deselected, environment_python=python_version,
    dependency_lock_sha256=lock_digest, grader_sha256=grader_digest, policy_sha256=policy_digest)
print('\nREVERTBENCH_BASELINE=' + json.dumps(payload, separators=(',', ':')), flush=True)
sys.exit(code)
"""


class BaselineResult(EvidenceModel):
    status: Literal["passed", "failed", "invalid_run"]
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


def parse_baseline_output(stdout: bytes, stderr: bytes, exit_code: int | None) -> BaselineResult:
    common = {
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "exit_code": exit_code,
    }
    try:
        lines = [line[len(_PREFIX) :] for line in stdout.splitlines() if line.startswith(_PREFIX)]
        if len(lines) != 1:
            raise ValueError("missing or duplicate baseline report")
        payload = json.loads(lines[0])
        result = BaselineResult.model_validate_json(
            json.dumps({**payload, **common, "status": "invalid_run"})
        )
    except (ValueError, TypeError, ValidationError, RecursionError):
        return BaselineResult.model_validate(
            {"status": "invalid_run", "reason": "baseline_report_invalid", **common}
        )
    if result.errors and any("SyntaxError:" in detail for detail in result.details):
        return result.model_copy(update={"status": "failed", "reason": "unsupported_environment"})
    if result.errors or exit_code in (2, 3, 4) or exit_code not in (0, 1, 5):
        return result.model_copy(update={"reason": "baseline_collection_or_worker_error"})
    counted = result.passed + result.failed + result.skipped + result.xfailed + result.xpassed
    if (
        len(result.node_ids) != result.collected
        or len(set(result.node_ids)) != result.collected
        or counted != result.collected
        or (exit_code == 0 and result.failed)
    ):
        return result.model_copy(update={"reason": "baseline_report_inconsistent"})
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
    ) -> None:
        self.image, self.client = image, client
        self.limits = limits or SandboxLimits()

    def __call__(self, tree: Path) -> BaselineResult:
        try:
            with AgentSandbox(self.image, tree, client=self.client, limits=self.limits) as box:
                execution = box.exec(
                    ["/usr/local/bin/python", "-I", "-c", _BASELINE_SCRIPT],
                    timeout_seconds=60,
                    output_limit_bytes=65_536,
                )
            if execution.status != "completed":
                return BaselineResult(status="invalid_run", reason=f"baseline_{execution.status}")
            return parse_baseline_output(execution.stdout, execution.stderr, execution.exit_code)
        except (SandboxError, OSError) as error:
            return BaselineResult(
                status="invalid_run",
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
