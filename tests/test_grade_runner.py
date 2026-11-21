"""Trusted synthetic submissions exercise the local backend, not an isolation boundary."""

from pathlib import Path

import pytest

from revertbench.grade.runner import run_tree


@pytest.fixture
def toy_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "toy"
    tree.mkdir()
    (tree / "toy.py").write_text("def normalize(value):\n    return value.strip()\n")
    return tree


def test_runner_records_failing_call_and_its_project_coverage(toy_tree: Path) -> None:
    result = run_tree(
        toy_tree,
        "from toy import normalize\ndef test_regression():\n    assert normalize(' X ') == 'x'\n",
        seed=11,
    )
    assert result.status == "completed", result.stderr
    assert result.exit_code == 1
    assert len(result.collected_node_ids) == 1
    test = result.tests[0]
    assert test.setup is not None and test.setup.outcome == "passed"
    assert test.call is not None and test.call.outcome == "failed"
    assert test.call.exception_type == "AssertionError"
    assert test.teardown is not None and test.teardown.outcome == "passed"
    assert test.covered_lines["toy.py"] == (2,)
    assert not (toy_tree / "tests" / "test_revertbench_submission.py").exists()


def test_runner_passes_fixed_behavior(toy_tree: Path) -> None:
    (toy_tree / "toy.py").write_text("def normalize(value):\n    return value.strip().lower()\n")
    result = run_tree(
        toy_tree,
        "from toy import normalize\ndef test_regression():\n    assert normalize(' X ') == 'x'\n",
        seed=11,
    )
    assert result.status == "completed", result.stderr
    assert result.exit_code == 0
    assert result.tests[0].call is not None and result.tests[0].call.outcome == "passed"


def test_runner_ignores_repository_conftest_config_and_environment(
    toy_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (toy_tree / "conftest.py").write_text("raise RuntimeError('must not load')\n")
    (toy_tree / "pytest.ini").write_text("[pytest]\naddopts = --nonsense-option\n")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--nonsense-option")
    result = run_tree(toy_tree, "def test_true():\n    assert True\n", seed=11)
    assert result.status == "completed", result.stderr
    assert result.exit_code == 0


def test_coverage_is_per_node_and_excludes_fixture_setup(toy_tree: Path) -> None:
    source = """import pytest
from toy import normalize

@pytest.fixture
def prepared():
    return normalize(' X ')

def test_calls_api():
    assert normalize(' X ') == 'X'

def test_unrelated(prepared):
    assert False
"""
    result = run_tree(toy_tree, source, seed=11)
    assert result.status == "completed", result.stderr
    results = {test.node_id.split("::")[-1]: test for test in result.tests}
    assert results["test_calls_api"].covered_lines["toy.py"] == (2,)
    assert "toy.py" not in results["test_unrelated"].covered_lines


@pytest.mark.parametrize("body", ["pytest.skip('skip')", "pytest.xfail('expected')"])
def test_skip_and_expected_failure_are_explicit(toy_tree: Path, body: str) -> None:
    result = run_tree(toy_tree, f"import pytest\ndef test_outcome():\n    {body}\n", seed=11)
    assert result.status == "completed", result.stderr
    assert result.tests[0].call is not None and result.tests[0].call.outcome == "skipped"
    assert result.tests[0].call.was_xfail == ("xfail" in body)


def test_unexpected_pass_is_explicit(toy_tree: Path) -> None:
    result = run_tree(
        toy_tree,
        "import pytest\n@pytest.mark.xfail\ndef test_outcome():\n    assert True\n",
        seed=11,
    )
    assert result.status == "completed", result.stderr
    assert result.tests[0].call is not None and result.tests[0].call.was_xfail


def test_collection_errors_are_not_test_failures(toy_tree: Path) -> None:
    result = run_tree(toy_tree, "def test_broken(:\n", seed=11)
    assert result.status == "completed", result.stderr
    assert result.collection_errors
    assert not result.tests


def test_missing_tree_is_infrastructure_invalid(tmp_path: Path) -> None:
    result = run_tree(tmp_path / "absent", "def test_true():\n    assert True\n", seed=11)
    assert result.status == "invalid_run"
    assert result.reason == "tree_unavailable"


def test_timeout_terminates_trusted_sleeping_submission(toy_tree: Path) -> None:
    result = run_tree(
        toy_tree,
        "import time\ndef test_sleep():\n    time.sleep(10)\n",
        seed=11,
        timeout_seconds=0.7,
    )
    assert result.status == "submission_timeout", result.stderr
    assert result.exit_code is not None


def test_output_cap_terminates_trusted_noisy_submission(toy_tree: Path) -> None:
    result = run_tree(
        toy_tree, "def test_noisy():\n    print('X' * 100000)\n", seed=11, output_limit_bytes=1024
    )
    assert result.status == "submission_output_limit", result.stderr
    assert result.truncated
    assert len(result.stdout.encode()) + len(result.stderr.encode()) <= 1024


def test_premature_exit_cannot_look_like_pass(toy_tree: Path) -> None:
    result = run_tree(toy_tree, "import os\ndef test_exit():\n    os._exit(0)\n", seed=11)
    assert result.status == "submission_error"
    assert result.reason == "incomplete_report"


def test_seeded_order_and_randomness_are_recorded(toy_tree: Path) -> None:
    source = "\n".join(f"def test_{index}():\n    assert True\n" for index in range(8))
    first = run_tree(toy_tree, source, seed=11)
    repeat = run_tree(toy_tree, source, seed=11)
    changed = run_tree(toy_tree, source, seed=37)
    assert first.status == repeat.status == changed.status == "completed"
    assert first.execution_order == repeat.execution_order
    assert first.execution_order != changed.execution_order
    assert set(first.execution_order) == set(first.collected_node_ids)


def test_each_run_starts_from_fresh_source(toy_tree: Path) -> None:
    source = (
        "from pathlib import Path\ndef test_state():\n"
        "    p = Path('state.txt')\n    assert not p.exists()\n"
        "    p.write_text('created')\n"
    )
    assert run_tree(toy_tree, source, seed=11).exit_code == 0
    assert run_tree(toy_tree, source, seed=23).exit_code == 0
    assert not (toy_tree / "state.txt").exists()


def test_worker_environment_does_not_copy_provider_secrets(
    toy_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-secret-never-for-worker")
    source = "import os\ndef test_env():\n    assert 'GROQ_API_KEY' not in os.environ\n"
    result = run_tree(toy_tree, source, seed=11)
    assert result.status == "completed", result.stderr
    assert result.exit_code == 0


def test_timeout_kills_controlled_descendant(toy_tree: Path, tmp_path: Path) -> None:
    import subprocess
    import time

    pid_file = tmp_path / "child.pid"
    source = f"""import subprocess
import sys
import time
from pathlib import Path

def test_child():
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])
    Path({str(pid_file)!r}).write_text(str(child.pid))
    time.sleep(10)
"""
    result = run_tree(toy_tree, source, seed=11, timeout_seconds=1.0)
    assert result.status == "submission_timeout", result.stderr
    child_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 1.0
    state = "running"
    while time.monotonic() < deadline:
        child = subprocess.run(
            ["ps", "-p", str(child_pid), "-o", "stat="],
            capture_output=True,
            text=True,
            check=False,
        )
        state = child.stdout.strip()
        if not state or state.startswith("Z"):
            break
        time.sleep(0.01)
    assert not state or state.startswith("Z"), f"descendant still active: {state}"


def test_worker_startup_failure_is_invalid(toy_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import revertbench.grade.runner as runner

    monkeypatch.setattr(runner, "_command", lambda tree, config: ["/nonexistent/python"])
    result = runner.run_tree(toy_tree, "def test_true():\n    assert True\n", seed=11)
    assert result.status == "invalid_run"
    assert result.reason == "worker_io_failure"


def test_malformed_worker_report_cannot_pass(toy_tree: Path) -> None:
    from revertbench.grade.runner import _Captured, _decode_report

    report = toy_tree / "report.json"
    report.with_suffix(".started").write_text("started")
    report.write_text('{"status": "completed", "forced_pass": true}')
    result = _decode_report(report, _Captured(0, "", ""), 11)
    assert result.status == "submission_error"
    assert result.reason == "malformed_report"


def test_inconsistent_worker_report_cannot_pass(toy_tree: Path) -> None:
    from revertbench.grade.runner import _Captured, _decode_report
    from revertbench.schema import ExecutionResult

    report = toy_tree / "report.json"
    report.with_suffix(".started").write_text("started")
    report.write_text(
        ExecutionResult(
            status="completed",
            seed=11,
            exit_code=0,
            collected_node_ids=("test_submission.py::test_missing",),
        ).model_dump_json()
    )
    result = _decode_report(report, _Captured(0, "", ""), 11)
    assert result.status == "submission_error"
    assert result.reason == "inconsistent_report"


@pytest.mark.parametrize("emission", ["print('🚀' * 2000)", "os.write(1, bytes([255]) * 2000)"])
def test_decoded_output_stays_within_byte_cap(toy_tree: Path, emission: str) -> None:
    source = f"import os\ndef test_output():\n    {emission}\n"
    result = run_tree(toy_tree, source, seed=11, output_limit_bytes=1001)
    assert result.status == "submission_output_limit"
    assert result.truncated
    assert len(result.stdout.encode()) + len(result.stderr.encode()) <= 1001


@pytest.mark.parametrize("link_kind", ["file", "tests_directory"])
def test_source_symlinks_are_rejected_before_copy(
    toy_tree: Path, tmp_path: Path, link_kind: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "host_file").write_text("must not copy")
    if link_kind == "file":
        (toy_tree / "external.py").symlink_to(outside / "host_file")
    else:
        (toy_tree / "tests").symlink_to(outside, target_is_directory=True)
    result = run_tree(toy_tree, "def test_true():\n    assert True\n", seed=11)
    assert result.status == "invalid_run"
    assert result.reason == "tree_symlink"
    assert not (outside / "test_revertbench_submission.py").exists()


def test_project_exception_cannot_impersonate_builtin_by_name(toy_tree: Path) -> None:
    (toy_tree / "toy.py").write_text(
        "class AssertionError(Exception):\n    pass\n\n"
        "def normalize(value):\n    raise AssertionError('project exception')\n"
    )
    source = "from toy import normalize\ndef test_exception():\n    normalize('x')\n"
    result = run_tree(toy_tree, source, seed=11)
    assert result.status == "completed", result.stderr
    assert result.tests[0].call is not None
    assert result.tests[0].call.exception_type == "toy.AssertionError"


@pytest.mark.parametrize("exit_code", [3, 4, -9, -15])
@pytest.mark.parametrize("report_state", ["missing", "malformed"])
def test_worker_failure_remains_invalid_after_startup(
    toy_tree: Path, exit_code: int, report_state: str
) -> None:
    from revertbench.grade.runner import _Captured, _decode_report

    report = toy_tree / "report.json"
    report.with_suffix(".started").write_text("started")
    if report_state == "malformed":
        report.write_text("not a complete report")
    result = _decode_report(report, _Captured(exit_code, "", ""), 11)
    assert result.status == "invalid_run"
    assert result.reason == ("pytest_internal_error" if exit_code in (3, 4) else "worker_lost")


@pytest.mark.parametrize("limit", ["timeout", "output"])
def test_attributed_limit_takes_precedence_over_kill_signal(toy_tree: Path, limit: str) -> None:
    from revertbench.grade.runner import _Captured, _decode_report

    report = toy_tree / "report.json"
    report.with_suffix(".started").write_text("started")
    capture = _Captured(-9, "", "", stopped="timeout" if limit == "timeout" else "output")
    result = _decode_report(report, capture, 11)
    assert result.status == (
        "submission_timeout" if limit == "timeout" else "submission_output_limit"
    )
