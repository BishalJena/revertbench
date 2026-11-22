"""Run existing repository suites only inside isolated containers."""

from __future__ import annotations

import base64
import binascii
import json
import zlib
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Final, Literal, Protocol

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
BaselineProfile = Literal[
    "pytest",
    "unittest",
    "boltons_upstream_default_behavior_v1",
    "pydash_upstream_default_behavior_v1",
    "blinker_upstream_default_behavior_v1",
    "babel_upstream_default_behavior_v1",
    "babel_cldr44_default_behavior_v1",
]
_BABEL_PROFILE: Final = "babel_upstream_default_behavior_v1"
_BABEL_CLDR44_PROFILE: Final = "babel_cldr44_default_behavior_v1"
_BABEL_BASELINE_MEMORY_BYTES = 512 * 1024**2
_BABEL_DATA: dict[BaselineProfile, tuple[str, str]] = {
    _BABEL_PROFILE: (
        "data/babel",
        "38c999568bebecfc77cb41875776beb97382dd55b3133aaaac73f6dd98c6cf5e",
    ),
    _BABEL_CLDR44_PROFILE: (
        "cldr44/data/babel",
        "1bcb4543e529f3ad7d26b1d5d6b596443b6c9429ce4488f5d22f0b0a8168a7d0",
    ),
}
_BABEL_SETUPTOOLS_SKIP = {"tests/messages/test_setuptools_frontend.py": "setuptools"}
_BABEL_COLLECTION_SKIPS: dict[BaselineProfile, dict[str, str]] = {
    _BABEL_PROFILE: {
        "tests/interop/test_jinja2_interop.py": "jinja2",
        **_BABEL_SETUPTOOLS_SKIP,
    },
    _BABEL_CLDR44_PROFILE: _BABEL_SETUPTOOLS_SKIP,
}
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

_PYTEST_PROFILE_ARGS: dict[str, tuple[str, ...]] = {
    "pytest": _PYTEST_ARGS,
    "boltons_upstream_default_behavior_v1": (
        *_PYTEST_ARGS[:-1],
        "--doctest-modules",
        "-o",
        "doctest_optionflags=ALLOW_UNICODE ELLIPSIS IGNORE_EXCEPTION_DETAIL NORMALIZE_WHITESPACE",
        "boltons",
        "tests",
    ),
    "pydash_upstream_default_behavior_v1": (
        *_PYTEST_ARGS[:-1],
        "--doctest-modules",
        "--ignore=tests/pytest_mypy_testing",
        "src/pydash",
        "tests",
    ),
    "blinker_upstream_default_behavior_v1": (
        "-v",
        "--tb=short",
        "-c",
        "/opt/revertbench/pytest.ini",
        "--rootdir",
        "/workspace/repo",
        "--basetemp=/tmp/revertbench-baseline",
        "-p",
        "no:cacheprovider",
        "-p",
        "pytest_asyncio.plugin",
        "-o",
        "filterwarnings=error",
        "-o",
        "asyncio_mode=auto",
        "-o",
        "asyncio_default_fixture_loop_scope=function",
        "tests",
    ),
    **dict.fromkeys(
        _BABEL_DATA,
        (
            "-q",
            "--tb=short",
            "-c",
            "/workspace/repo/setup.cfg",
            "--rootdir",
            "/workspace/repo",
            "--basetemp=/tmp/revertbench-baseline",
            "-p",
            "no:cacheprovider",
            ".",
        ),
    ),
}


def baseline_command(profile: BaselineProfile) -> tuple[str, ...]:
    """Return the fixed effective test command; a trusted reporting shim runs it."""
    if not isinstance(profile, str):
        raise ValueError("unknown fixed baseline profile")
    if profile in _PYTEST_PROFILE_ARGS:
        return ("/usr/local/bin/python", "-m", "pytest", *_PYTEST_PROFILE_ARGS[profile])
    if profile == "unittest":
        return ("/usr/local/bin/python", "-m", "unittest", "-q")
    raise ValueError("unknown fixed baseline profile")


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

_BABEL_SCRIPT = r"""
import re
from importlib import metadata

exact_runtime_skips = {
    'babel/numbers.py::babel.numbers.format_number': (
        'call', 'all tests skipped by +SKIP option',
    ),
    'tests/test_localedata.py::test_reserved_locale_names': ('setup', 'windows-only test'),
    **{
        f'tests/test_support.py::TranslationsTestCase::{name}': ('setup', 'lgettext is deprecated')
        for name in ('test_lpgettext', 'test_lnpgettext', 'test_ldpgettext', 'test_ldnpgettext')
    },
}
if profile == 'babel_upstream_default_behavior_v1':
    exact_runtime_skips['tests/test_localtime.py::test_issue_1092_with_pytz'] = (
        'call', 'pytz is not installed',
    )

pytz_fixture_nodes = {
    'tests/test_date_intervals.py::test_format_interval_in_tz',
    'tests/test_support.py::TestFormat::test_format_datetime',
    'tests/test_support.py::TestFormat::test_format_time',
    *{f'tests/test_dates.py::{name}' for name in (
        'test_get_timezone_gmt', 'test_get_timezone_location', 'test_get_timezone_name_tzinfo',
        'test_get_timezone_name_time_pytz', 'test_get_timezone_name_misc', 'test_format_datetime',
        'test_format_time', 'test_format_skeleton',
        'test_no_inherit_metazone_marker_never_in_output', 'test_no_inherit_metazone_formatting',
    )},
}

def prepare_babel():
    overlay = Path('/opt/revertbench/babel')
    deps = overlay / 'deps'
    path, data_digest = __FIXED_BABEL_DATA__
    relative_data = Path(path)
    data = overlay / relative_data
    checkout = Path(root)
    package = checkout / 'babel'
    source_locales = data / 'locale-data'
    target_locales = package / 'locale-data'
    data_parents = (overlay / parent for parent in relative_data.parents)
    directories = (*data_parents, deps, data, source_locales,
        checkout, package, target_locales)
    if any(path.is_symlink() or not path.is_dir() for path in directories):
        raise ValueError('Babel runtime directory mismatch')
    config = checkout / 'setup.cfg'
    if config.is_symlink() or not config.is_file():
        raise ValueError('Babel native configuration missing')
    distributions = sorted((item.metadata['Name'].lower().replace('_', '-'), item.version)
        for item in metadata.distributions(path=[str(deps)]))
    if distributions != [('freezegun', '1.5.1'), ('python-dateutil', '2.9.0.post0')]:
        raise ValueError('Babel runtime dependency mismatch')
    if {path.name for path in data.iterdir()} != {'global.dat', 'locale-data'}:
        raise ValueError('Babel locale data layout mismatch')
    locales = sorted(source_locales.iterdir())
    if any(not re.fullmatch(r'[A-Za-z0-9_]+\.dat', path.name) for path in locales):
        raise ValueError('Babel locale data filename mismatch')
    if any(path.suffix == '.dat' for path in target_locales.iterdir()):
        raise ValueError('Babel locale target already contains data')
    manifest = hashlib.sha256()
    copies = []
    for source in [data / 'global.dat', *locales]:
        relative = source.relative_to(data.parent)
        target = checkout / relative
        if source.is_symlink() or not source.is_file() or target.is_symlink() or target.exists():
            raise ValueError('Babel locale file mismatch or collision')
        content = source.read_bytes()
        manifest.update(f'{relative.as_posix()} {hashlib.sha256(content).hexdigest()}\n'.encode())
        copies.append((target, content))
    if manifest.hexdigest() != data_digest:
        raise ValueError('Babel locale data digest mismatch')
    for target, content in copies:
        with target.open('xb') as stream:
            stream.write(content)
    sys.path[:0] = [root, str(deps)]

def babel_skip_reason(report):
    description = report.longrepr
    if isinstance(description, tuple) and len(description) == 3:
        return str(description[2]).removeprefix('Skipped: ')
    return ''

def babel_runtime_skip_allowed(report):
    reason = babel_skip_reason(report)
    if report.nodeid in exact_runtime_skips:
        return (report.when, reason) == exact_runtime_skips[report.nodeid]
    node, bracket, params = report.nodeid.partition('[')
    return (node in pytz_fixture_nodes and bracket and params.endswith(']')
        and 'pytz.timezone' in params[:-1].split('-')
        and report.when == 'setup' and reason == 'pytz not available')
"""

_BASELINE_SCRIPT = (
    _ENVIRONMENT_SCRIPT
    + r"""
import pytest
root = '/workspace/repo'
profile = __FIXED_PROFILE__
collection_skip_dependencies = __FIXED_BABEL_COLLECTION_SKIPS__
__FIXED_BABEL_SCRIPT__
class Evidence:
    def __init__(self):
        self.nodes = []
        self.calls = {}
        self.skips = set()
        self.xfails = set()
        self.xpasses = set()
        self.errors = []
        self.deselected = 0
        self.collection_skips = set()
    def pytest_collection_finish(self, session):
        self.nodes = [item.nodeid for item in session.items]
        if profile == 'blinker_upstream_default_behavior_v1':
            from importlib import metadata
            config = session.config
            allowed = config.pluginmanager.getplugin('pytest_asyncio.plugin')
            origin = Path(getattr(allowed, '__file__', '')).resolve()
            if (metadata.version('pytest-asyncio') != '1.5.0'
                    or not origin.is_relative_to(Path('/usr/local/lib/python3.12/site-packages'))
                    or config.getini('filterwarnings') != ['error']
                    or config.getini('asyncio_mode') != 'auto'
                    or config.getini('asyncio_default_fixture_loop_scope') != 'function'):
                self.errors.append('baseline async plugin or settings mismatch')
                pytest.exit('baseline trusted async runtime mismatch', returncode=4)
    def pytest_deselected(self, items):
        self.deselected += len(items)
    def pytest_collectreport(self, report):
        if report.failed:
            self.errors.append(str(report.longrepr)[:2048])
        elif collection_skip_dependencies and report.skipped:
            dependency = collection_skip_dependencies.get(report.nodeid)
            expected = f"could not import '{dependency}': No module named '{dependency}'"
            if (dependency and babel_skip_reason(report) == expected
                    and report.nodeid not in self.collection_skips):
                self.collection_skips.add(report.nodeid)
            else:
                self.errors.append(f'unexpected Babel skip during collection: {report.nodeid}')
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
            if collection_skip_dependencies and not babel_runtime_skip_allowed(report):
                self.errors.append(f'unexpected Babel skip during {report.when}: {node}')
        elif report.when != 'call' and report.outcome == 'failed':
            self.errors.append(str(report.longrepr)[:2048])

plugin = Evidence()
command = __FIXED_COMMAND__
if collection_skip_dependencies:
    try:
        prepare_babel()
    except (OSError, ValueError, TypeError, AttributeError) as error:
        plugin.errors.append(f'Babel native runtime preparation failed: {error}')
else:
    sys.path[:0] = [root + '/src', root]
code = 4 if plugin.errors else int(pytest.main(command[3:], plugins=[plugin]))
passed = sum(value == 'passed' and node not in plugin.xpasses
    for node, value in plugin.calls.items())
failed = sum(value == 'failed' for value in plugin.calls.values())
payload = dict(profile=profile, command=command, collected=len(plugin.nodes),
    passed=passed, failed=failed,
    skipped=len(plugin.skips) + len(plugin.collection_skips),
    xfailed=len(plugin.xfails), xpassed=len(plugin.xpasses),
    errors=len(plugin.errors), details=plugin.errors, node_ids=plugin.nodes,
    deselected=plugin.deselected, environment_python=python_version,
    dependency_lock_sha256=lock_digest, grader_sha256=grader_digest, policy_sha256=policy_digest)
if collection_skip_dependencies:
    payload['extra_skip_node_ids'] = sorted(plugin.collection_skips)
__FIXED_REPORT_OUTPUT__
sys.exit(code)
"""
)


def _pytest_script(profile: BaselineProfile) -> str:
    command = baseline_command(profile)
    if profile == "unittest":
        raise ValueError("unittest is not a pytest profile")
    report = (
        "print('\\nREVERTBENCH_BASELINE=' + json.dumps(payload, separators=(',', ':')), flush=True)"
    )
    if profile != "pytest":
        report = (
            "import base64, zlib\n"
            "encoded = base64.b64encode(zlib.compress("
            "json.dumps(payload, separators=(',', ':')).encode()))\n"
            "print('\\nREVERTBENCH_BASELINE_ZLIB=' + encoded.decode('ascii'), flush=True)"
        )
    return (
        _BASELINE_SCRIPT.replace("__FIXED_COMMAND__", repr(list(command)))
        .replace("__FIXED_PROFILE__", repr(profile))
        .replace(
            "__FIXED_BABEL_COLLECTION_SKIPS__",
            repr(_BABEL_COLLECTION_SKIPS.get(profile, {})),
        )
        .replace("__FIXED_BABEL_SCRIPT__", _BABEL_SCRIPT if profile in _BABEL_DATA else "")
        .replace("__FIXED_BABEL_DATA__", repr(_BABEL_DATA.get(profile)))
        .replace("__FIXED_REPORT_OUTPUT__", report)
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
    expected_command = baseline_command(expected_profile)
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
    if expected_profile not in ("pytest", "unittest") and result.command != expected_command:
        return result.model_copy(update={"reason": "baseline_command_mismatch"})
    if result.errors and any("SyntaxError:" in detail for detail in result.details):
        return result.model_copy(update={"status": "failed", "reason": "unsupported_environment"})
    if (
        result.collection_errors
        or (result.profile != "unittest" and result.errors)
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
    if result.profile in _BABEL_COLLECTION_SKIPS:
        inconsistent |= (
            counted - len(result.extra_skip_node_ids) != result.collected
            or result.extra_skip_node_ids != tuple(sorted(_BABEL_COLLECTION_SKIPS[result.profile]))
            or bool(result.xfailed or result.xpassed)
        )
    elif result.profile != "unittest":
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
        if profile in _BABEL_DATA:
            # The native all-locale suite and generated data exceed the default memory cap.
            self.limits = replace(self.limits, memory_bytes=_BABEL_BASELINE_MEMORY_BYTES)

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
                            _UNITTEST_SCRIPT
                            if self.profile == "unittest"
                            else _pytest_script(self.profile),
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
