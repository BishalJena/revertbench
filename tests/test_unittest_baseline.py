"""The explicit unittest profile includes nested test packages and load_tests doctests."""

from pathlib import Path

import docker
import pytest

from revertbench.validate.baseline import DockerBaselineRunner


def _nested_suite(tree: Path, *, broken: bool = False) -> None:
    (tree / "pkg/tests").mkdir(parents=True)
    (tree / "pkg/__init__.py").write_text("")
    (tree / "pkg/tests/__init__.py").write_text("")
    (tree / "pkg/library.py").write_text(
        '"""Example.\n\n>>> normalize(" X ")\n\'x\'\n"""\n'
        "def normalize(value):\n    return value.strip().lower()\n"
    )
    source = (
        "import doctest\nfrom unittest import TestCase\nfrom pkg import library\n"
        "class ApiTests(TestCase):\n    def test_normalize(self):\n"
        '        self.assertEqual(library.normalize(" X "), "x")\n'
        "def load_tests(loader, tests, pattern):\n"
        "    tests.addTests(doctest.DocTestSuite(library))\n    return tests\n"
    )
    if broken:
        source = 'raise ImportError("trusted synthetic unavailable support")\n'
    (tree / "pkg/tests/test_api.py").write_text(source)


@pytest.mark.docker
def test_unittest_discovers_nested_package_and_doctest(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    tree = tmp_path / "source"
    _nested_suite(tree)
    assert not (tree / "tests").exists()
    result = DockerBaselineRunner(worker_image, client=docker_client, profile="unittest")(tree)
    assert result.status == "passed", result.model_dump_json()
    assert result.profile == "unittest"
    assert result.command == ("/usr/local/bin/python", "-m", "unittest", "-q")
    assert result.collected == result.passed == 2
    assert set(result.node_ids) == {"pkg.tests.test_api.ApiTests.test_normalize", "pkg.library"}
    assert result.grader_sha256 and result.policy_sha256 and result.dependency_lock_sha256


@pytest.mark.docker
def test_unittest_loader_error_is_not_a_passing_baseline(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    _nested_suite(tmp_path / "source", broken=True)
    result = DockerBaselineRunner(worker_image, client=docker_client, profile="unittest")(
        tmp_path / "source"
    )
    assert result.status == "invalid_run"
    assert result.errors == 1
    assert any("ImportError" in detail for detail in result.details)


@pytest.mark.docker
def test_unittest_zero_suite_is_not_a_case_pass(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    result = DockerBaselineRunner(worker_image, client=docker_client, profile="unittest")(tmp_path)
    assert result.status == "failed"
    assert result.reason == "no_executed_tests"
    assert result.collected == result.passed == 0


@pytest.mark.docker
def test_worker_image_provides_declared_six_dependency(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    from revertbench.sandbox.agent import AgentSandbox

    with AgentSandbox(worker_image, tmp_path, client=docker_client) as box:
        result = box.exec(
            [
                "/usr/local/bin/python",
                "-I",
                "-c",
                "from six import moves; assert list(moves.range(3)) == [0, 1, 2]; "
                "import six; print(six.__version__)",
            ]
        )
    assert result.exit_code == 0, result.stderr_text
    assert result.stdout_text.strip() == "1.17.0"


def test_unittest_complete_id_report_can_exceed_console_cap() -> None:
    import base64
    import json
    import zlib

    from revertbench.validate.baseline import baseline_command, parse_baseline_output

    nodes = [f"pkg.tests.test_cases.LongCaseName.test_behavior_{number}" for number in range(2500)]
    payload = {
        "profile": "unittest",
        "command": baseline_command("unittest"),
        "collected": len(nodes),
        "passed": len(nodes),
        "node_ids": nodes,
    }
    raw = json.dumps(payload).encode()
    assert len(raw) > 65536
    packed = base64.b64encode(zlib.compress(raw))
    result = parse_baseline_output(
        b"REVERTBENCH_BASELINE_ZLIB=" + packed, b"", 0, expected_profile="unittest"
    )
    assert result.status == "passed"
    assert result.node_ids == tuple(nodes)


@pytest.mark.docker
def test_unittest_runtime_error_fails_existing_suite(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    _nested_suite(tmp_path / "source")
    (tmp_path / "source/pkg/tests/test_api.py").write_text(
        "from unittest import TestCase\nclass ApiTests(TestCase):\n"
        '    def test_error(self):\n        raise RuntimeError("trusted failing existing test")\n'
    )
    result = DockerBaselineRunner(worker_image, client=docker_client, profile="unittest")(
        tmp_path / "source"
    )
    assert result.status == "failed"
    assert result.reason == "existing_suite_failed"
    assert result.errors == 1 and not result.collection_errors


@pytest.mark.parametrize("updates", [{"passed": 2}, {"passed": 1, "skipped": 2}])
def test_successful_unittest_counts_must_account_for_executed_parents(
    updates: dict[str, int],
) -> None:
    import json

    from revertbench.validate.baseline import parse_baseline_output

    payload = {"profile": "unittest", "collected": 1, "passed": 1, "node_ids": ["one"], **updates}
    result = parse_baseline_output(
        b"REVERTBENCH_BASELINE=" + json.dumps(payload).encode(), b"", 0, expected_profile="unittest"
    )
    assert result.status == "invalid_run"
    assert result.reason == "baseline_report_inconsistent"


@pytest.mark.docker
def test_unittest_represents_additional_subtest_and_class_skip_events(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    _nested_suite(tmp_path / "source")
    (tmp_path / "source/pkg/tests/test_api.py").write_text(
        "from unittest import TestCase, SkipTest\nclass ApiTests(TestCase):\n"
        "    def test_good(self):\n        self.assertEqual(1, 1)\n"
        "    def test_skips(self):\n        for number in (1, 2):\n"
        "            with self.subTest(number=number):\n"
        '                self.skipTest("maintainer skip")\n'
        "class ClassSkip(TestCase):\n    @classmethod\n    def setUpClass(cls):\n"
        '        raise SkipTest("class prerequisite unavailable")\n'
        "    def test_unreached(self):\n        pass\n"
    )
    result = DockerBaselineRunner(worker_image, client=docker_client, profile="unittest")(
        tmp_path / "source"
    )
    assert result.status == "passed", result.model_dump_json()
    assert result.collected == 2 and result.skipped == 3
    assert len(result.extra_skip_node_ids) == 2


@pytest.mark.docker
def test_unittest_multiple_subtest_failures_still_fail_the_suite(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    _nested_suite(tmp_path / "source")
    (tmp_path / "source/pkg/tests/test_api.py").write_text(
        "from unittest import TestCase\nclass ApiTests(TestCase):\n"
        "    def test_errors(self):\n        for number in (1, 2):\n"
        "            with self.subTest(number=number):\n"
        "                self.assertEqual(number, 0)\n"
    )
    result = DockerBaselineRunner(worker_image, client=docker_client, profile="unittest")(
        tmp_path / "source"
    )
    assert result.status == "failed"
    assert result.collected == 1 and result.failed == 2
    assert result.reason == "existing_suite_failed"
