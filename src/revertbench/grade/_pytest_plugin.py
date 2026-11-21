"""Trusted pytest instrumentation; reports remain evidence, not in-process authenticity proof."""

import os
import random
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path

import coverage
import pytest

from revertbench.schema import ExecutionResult, PhaseResult, TestResult


@dataclass
class _State:
    root: Path
    report: Path
    seed: int
    collected: tuple[str, ...] = ()
    order: list[str] = field(default_factory=list)
    phases: dict[str, dict[str, PhaseResult]] = field(default_factory=dict)
    covered: dict[str, dict[str, tuple[int, ...]]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


_STATE: _State | None = None


def _state() -> _State:
    if _STATE is None:
        raise RuntimeError("grading plugin did not start")
    return _STATE


def _project_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return f"external:{path.name}"


def pytest_sessionstart(session: pytest.Session) -> None:
    global _STATE
    root = Path(os.environ["REVERTBENCH_TREE"]).resolve()
    report = Path(os.environ["REVERTBENCH_REPORT"])
    seed = int(os.environ["REVERTBENCH_SEED"])
    _STATE = _State(root=root, report=report, seed=seed)
    random.seed(seed)
    report.with_suffix(".started").write_text("started", encoding="utf-8")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    state = _state()
    state.collected = tuple(item.nodeid for item in items)
    random.Random(state.seed).shuffle(items)


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if report.failed:
        _state().errors.append(str(report.longrepr)[:4096])


def pytest_runtest_logstart(nodeid: str) -> None:
    _state().order.append(nodeid)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, None, None]:
    state = _state()
    measure = coverage.Coverage(
        data_file=None,
        config_file=False,
        source=[str(state.root)],
        omit=["*/test_revertbench_submission.py"],
        context=item.nodeid,
    )
    measure.set_option("run:disable_warnings", ["no-data-collected"])
    measure.start()
    try:
        yield
    finally:
        measure.stop()
        data = measure.get_data()
        state.covered[item.nodeid] = {
            _project_path(Path(path), state.root): tuple(sorted(lines))
            for path in data.measured_files()
            if (lines := data.lines(path))
        }


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    exception_type = None
    if call.excinfo is not None:
        exception = call.excinfo.type
        exception_type = (
            exception.__name__
            if exception.__module__ == "builtins"
            else (f"{exception.__module__}.{exception.__qualname__}")
        )
    paths = (
        ()
        if call.excinfo is None
        else tuple(
            _project_path(Path(str(entry.path)), _state().root) for entry in call.excinfo.traceback
        )
    )
    result = PhaseResult(
        outcome=report.outcome,
        exception_type=exception_type,
        traceback_paths=paths,
        was_xfail=hasattr(report, "wasxfail") or str(report.longrepr).startswith("[XPASS"),
    )
    _state().phases.setdefault(item.nodeid, {})[report.when] = result
    return report


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    state = _state()
    tests = tuple(
        TestResult(
            node_id=node,
            setup=phases.get("setup"),
            call=phases.get("call"),
            teardown=phases.get("teardown"),
            covered_lines=state.covered.get(node, {}),
        )
        for node, phases in state.phases.items()
    )
    report = ExecutionResult(
        status="completed",
        seed=state.seed,
        exit_code=int(exitstatus),
        collected_node_ids=state.collected,
        execution_order=tuple(state.order),
        tests=tests,
        collection_errors=tuple(state.errors),
    )
    pending = state.report.with_suffix(".pending")
    pending.write_text(report.model_dump_json(), encoding="utf-8")
    pending.replace(state.report)
