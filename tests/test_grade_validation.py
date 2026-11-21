"""Three-tree validation must isolate the original regression from later history."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from docker import DockerClient

from revertbench.grade import validation
from revertbench.grade.policy import PolicyResult
from revertbench.grade.rules import grade_submission
from revertbench.schema import ExecutionResult, PhaseResult
from revertbench.schema import TestResult as NodeResult

NODE = "tests/test_submission.py::test_regression"
SUBMISSION = (
    "from project import normalize\n\ndef test_regression():\n    assert normalize(' X ') == 'x'\n"
)


def _trees(tmp_path: Path) -> tuple[Path, Path, Path]:
    base, bad, repaired = (tmp_path / name for name in ("base", "bad", "repaired"))
    for tree in (base, bad, repaired):
        tree.mkdir()
        body = "value" if tree == bad else "value.strip().lower()"
        (tree / "project.py").write_text(f"def normalize(value):\n    return {body}\n")
    return base, bad, repaired


def _execution(seed: int, *, failed: bool = False, node: str = NODE) -> ExecutionResult:
    return ExecutionResult(
        status="completed",
        seed=seed,
        exit_code=1 if failed else 0,
        collected_node_ids=(node,),
        execution_order=(node,),
        tests=(
            NodeResult(
                node_id=node,
                setup=PhaseResult(outcome="passed"),
                call=PhaseResult(
                    outcome="failed" if failed else "passed",
                    exception_type="AssertionError" if failed else None,
                ),
                teardown=PhaseResult(outcome="passed"),
                covered_lines={"project.py": (2,)},
            ),
        ),
    )


@pytest.mark.parametrize("base_result", ["failed", "different_collection", "invalid_run", "passed"])
def test_base_must_pass_same_reference_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_result: str
) -> None:
    base, bad, repaired = _trees(tmp_path)

    def run(
        tree: Path,
        submission: str,
        *,
        seed: int,
        timeout_seconds: float = 10,
        output_limit_bytes: int = 65536,
    ) -> ExecutionResult:
        if tree == base:
            if base_result == "invalid_run":
                return ExecutionResult(status="invalid_run", seed=seed, reason="image_missing")
            return _execution(
                seed,
                failed=base_result == "failed",
                node="tests/test_submission.py::different"
                if base_result == "different_collection"
                else NODE,
            )
        return _execution(seed, failed=tree == bad)

    monkeypatch.setattr(validation, "run_tree", run)
    monkeypatch.setattr("revertbench.grade.rules.run_tree", run)
    result = validation.validate_reference(base, bad, repaired, SUBMISSION, {"project.py": {2}})

    if base_result == "invalid_run":
        assert result.status == "invalid_run"
        assert result.score is None
    elif base_result == "passed":
        assert result.status == "passed"
        assert result.score == 1
        assert result.conditions["base_passes"]
        assert len(result.executions) == 9
    else:
        assert result.status == "rejected"
        assert result.score == 0
        assert result.reason in {"reference_failed_on_base", "collection_mismatch"}


def test_toy_reference_passes_base_fails_bad_passes_minimal_repair(tmp_path: Path) -> None:
    base, bad, repaired = _trees(tmp_path)
    result = validation.validate_reference(base, bad, repaired, SUBMISSION, {"project.py": {2}})
    assert result.status == "passed", result.model_dump()
    assert result.score == 1


def test_unrelated_historical_fix_change_cannot_earn_differential_credit(tmp_path: Path) -> None:
    base, bad, repaired = _trees(tmp_path)
    historical = tmp_path / "historical_fix"
    historical.mkdir()
    for tree in (base, bad, repaired):
        with (tree / "project.py").open("a") as stream:
            stream.write("\ndef unrelated():\n    return 'old'\n")
    (historical / "project.py").write_text(
        "def normalize(value):\n    return value.strip().lower()\n"
        "\ndef unrelated():\n    return 'new'\n"
    )
    submission = (
        "from project import normalize, unrelated\n\ndef test_regression():\n"
        "    normalize(' X ')\n    assert unrelated() == 'new'\n"
    )

    result = validation.validate_reference(base, bad, repaired, submission, {"project.py": {2}})

    assert result.score == 0
    assert result.status == "rejected"

    # The same coverage-plus-version test passes a confounded historical pair,
    # but fails the controlled pair even without the base-tree validation gate.
    assert grade_submission(bad, historical, submission, {"project.py": {2}}).score == 1
    assert grade_submission(bad, repaired, submission, {"project.py": {2}}).score == 0


def test_reference_policy_access_failure_has_no_model_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, bad, repaired = _trees(tmp_path)

    def unavailable(source: str, tree: Path) -> PolicyResult:
        return PolicyResult(False, "project_unavailable", ("case source inaccessible",))

    monkeypatch.setattr(validation, "check_submission", unavailable)
    result = validation.validate_reference(base, bad, repaired, SUBMISSION, {"project.py": {2}})
    assert result.status == "invalid_run"
    assert result.score is None


def test_selected_backend_handles_all_nine_reference_runs_without_host_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, bad, repaired = _trees(tmp_path)
    calls: list[tuple[Path, int, float, int]] = []

    def host_forbidden(
        tree: Path,
        submission: str,
        *,
        seed: int,
        timeout_seconds: float = 10,
        output_limit_bytes: int = 65536,
    ) -> ExecutionResult:
        raise AssertionError("reference validation must not fall back to host execution")

    def selected(
        tree: Path,
        submission: str,
        *,
        seed: int,
        timeout_seconds: float = 10,
        output_limit_bytes: int = 65536,
    ) -> ExecutionResult:
        assert submission == SUBMISSION
        calls.append((tree, seed, timeout_seconds, output_limit_bytes))
        return _execution(seed, failed=tree == bad)

    monkeypatch.setattr(validation, "run_tree", host_forbidden)
    monkeypatch.setattr("revertbench.grade.rules.run_tree", host_forbidden)
    result = validation.validate_reference(
        base,
        bad,
        repaired,
        SUBMISSION,
        {"project.py": {2}},
        timeout_seconds=21,
        output_limit_bytes=4096,
        execution_backend=selected,
    )
    assert result.status == "passed"
    assert len(result.executions) == 9
    assert set(calls) == {
        (tree, seed, 21, 4096) for tree in (base, bad, repaired) for seed in (11, 23, 37)
    }
    assert len(calls) == 9


def _docker_gate_probe(
    tmp_path: Path, *, enabled: bool, daemon_available: bool, request_error: bool = False
) -> subprocess.CompletedProcess[str]:
    directory = tmp_path / "gate"
    directory.mkdir()
    (directory / "pytest.ini").write_text("[pytest]\nmarkers = docker: isolated Docker checks\n")
    plugin = Path(__file__).with_name("conftest.py").read_text()
    if daemon_available:
        fake = (
            "\nclass _FakeClient:\n"
            "    def ping(self): return True\n"
            "    def close(self): pass\n"
            "import docker\ndocker.from_env = lambda **kwargs: _FakeClient()\n"
        )
    else:
        exception = (
            "requests.exceptions.ConnectionError"
            if request_error
            else "docker.errors.DockerException"
        )
        fake = (
            "\nimport docker\nimport requests\n"
            "def _unavailable(**kwargs):\n"
            f"    raise {exception}('synthetic unavailable daemon')\n"
            "docker.from_env = _unavailable\n"
        )
    (directory / "conftest.py").write_text(plugin + fake)
    (directory / "test_gate.py").write_text(
        "import pytest\n@pytest.mark.docker\ndef test_must_run():\n    assert False\n"
    )
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-c",
        str(directory / "pytest.ini"),
        str(directory),
    ]
    if enabled:
        command.append("--run-docker")
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""}
    return subprocess.run(command, capture_output=True, text=True, timeout=15, env=env)


def test_docker_checks_are_skipped_without_explicit_opt_in(tmp_path: Path) -> None:
    result = _docker_gate_probe(tmp_path, enabled=False, daemon_available=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 skipped" in result.stdout


def test_docker_opt_in_requires_a_daemon_instead_of_silent_skip(tmp_path: Path) -> None:
    result = _docker_gate_probe(tmp_path, enabled=True, daemon_available=False)
    assert result.returncode != 0
    assert "Docker" in result.stdout + result.stderr
    assert "1 skipped" not in result.stdout


def test_docker_opt_in_actually_runs_marked_checks(tmp_path: Path) -> None:
    result = _docker_gate_probe(tmp_path, enabled=True, daemon_available=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 failed" in result.stdout
    assert "1 skipped" not in result.stdout


def test_docker_transport_failure_is_a_clear_required_gate_error(tmp_path: Path) -> None:
    result = _docker_gate_probe(tmp_path, enabled=True, daemon_available=False, request_error=True)
    assert result.returncode != 0
    assert "Docker must be reachable" in result.stdout + result.stderr
    assert "synthetic unavailable daemon" not in result.stdout + result.stderr


@pytest.mark.docker
def test_docker_backend_validates_base_bad_and_repair_with_one_rule_engine(
    tmp_path: Path, docker_client: DockerClient, worker_image: str
) -> None:
    from revertbench.sandbox.worker import DockerRunner

    base, bad, repaired = _trees(tmp_path)
    result = validation.validate_reference(
        base,
        bad,
        repaired,
        SUBMISSION,
        {"project.py": {2}},
        execution_backend=DockerRunner(worker_image, client=docker_client),
    )
    assert result.status == "passed", result.model_dump()
    assert result.score == 1
    assert result.conditions["base_passes"]
    assert len(result.executions) == 9
