"""Resource choices are replayed only by a pinned engine that supports them."""

from pathlib import Path
from typing import Any

import pytest


def _helper(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from scripts import reproduce_case

    return reproduce_case


def test_reproduction_legacy_engine_uses_implicit_64_and_new_engine_receives_recorded_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _helper(monkeypatch)

    def legacy(built: object, *, image: str) -> None:
        pass

    def current(
        built: object,
        *,
        image: str,
        baseline_profile: str = "pytest",
        baseline_pids_limit: int = 64,
    ) -> None:
        pass

    assert helper._baseline_options(legacy, {}) == {}
    assert helper._baseline_options(current, {}) == {
        "baseline_profile": "pytest",
        "baseline_pids_limit": 64,
    }
    assert helper._baseline_options(
        current, {"baseline_profile": "unittest", "baseline_pids_limit": 256}
    ) == {"baseline_profile": "unittest", "baseline_pids_limit": 256}
    with pytest.raises(helper.ReproductionError, match=r"PID|resource"):
        helper._baseline_options(legacy, {"baseline_pids_limit": 256})


@pytest.mark.parametrize("value", [7, 257, True, "256", 256.0])
def test_reproduction_cannot_coerce_recorded_pid_limit(
    monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    helper = _helper(monkeypatch)
    with pytest.raises(helper.ReproductionError):
        helper._baseline_options(lambda **kwargs: None, {"baseline_pids_limit": value})


def test_official_supplement_uses_recorded_baseline_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from revertbench.sandbox import agent
    from revertbench.sandbox.agent import AgentExecResult

    helper = _helper(monkeypatch)
    seen: dict[str, Any] = {}

    class Box:
        def __init__(self, *args: object, **kwargs: Any) -> None:
            seen.update(kwargs)

        def __enter__(self) -> "Box":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def exec(self, argv: list[str], **kwargs: Any) -> AgentExecResult:
            assert kwargs == {"timeout_seconds": 60, "output_limit_bytes": 65536}
            return AgentExecResult(
                status="completed", exit_code=0, stderr=b"Ran 1 test in 11.0s\n\nOK\n"
            )

    monkeypatch.setattr(agent, "AgentSandbox", Box)
    result = helper._official_suite(
        tmp_path,
        "synthetic-image",
        ("/usr/local/bin/python", "-m", "unittest", "-q"),
        object(),
        baseline_pids_limit=256,
    )
    assert result["pids_limit"] == 256
    assert seen["limits"].pids == 256 and seen["limits"].memory_bytes == 268435456
