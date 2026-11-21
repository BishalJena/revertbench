"""Saved-test completion is an explicit prospective contract, never error salvage."""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from revertbench.harness.episode import EpisodeIdentity, run_episode
from revertbench.models.budget import CostBudget
from revertbench.models.client import ProviderError, ProviderResponse
from revertbench.models.config import ModelConfig
from revertbench.models.ledger import JsonlLedger
from revertbench.models.scheduler import RateScheduler
from test_harness_episode import Crash, FakeClient, FakeRuntime, model_config, response

POLICY = "saved_test_on_stop_v1"
SOURCE = "def test_ok():\n    assert True\n"


class Runtime(FakeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.graded: list[bytes | None] = []
        self.reject_write: str | None = None
        self.bad_ack = False
        self.bad_read = 0
        self.reads = 0
        self.policy_allowed = True

    def tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "write_test" and args["content"] == self.reject_write:
            return {"ok": False, "error": "invalid_content"}
        if name == "submit" and self.bad_ack:
            return {"submitted": False}
        result = super().tool(name, args)
        if name == "write_test":
            result["policy"] = {"allowed": self.policy_allowed}
        return result

    def get_submission(self) -> bytes:
        self.reads += 1
        if self.reads == self.bad_read:
            return b"wrong bytes"
        return super().get_submission()

    def grade(self) -> dict[str, Any]:
        self.graded.append(self.source)
        return super().grade()


def execute(
    path: Path,
    client: FakeClient,
    runtime: Runtime,
    *,
    policy: Any = POLICY,
    config: ModelConfig | None = None,
    crash_event: str | None = None,
    budget_usd: Decimal = Decimal("0.25"),
) -> dict[str, Any]:
    class Ledger(JsonlLedger):
        def append(self, event: str, **data: Any) -> dict[str, Any]:
            row = super().append(event, **data)
            if event == crash_event:
                raise Crash()
            return row

    with (
        JsonlLedger(path / "project.jsonl") as project,
        Ledger(path / "trajectory.jsonl") as ledger,
    ):
        runtime.ledger = ledger
        return run_episode(
            identity=EpisodeIdentity("experiment", "episode-1", "sample"),
            config=config or model_config(),
            client=client,
            runtime=runtime,
            ledger=ledger,
            budget=CostBudget(project, "experiment", budget_usd),
            scheduler=RateScheduler(
                project, "model", rpm=1000, tpm=10000000, rpd=10000, tpd=100000000
            ),
            clock=lambda: 1000.0,
            completion_policy=policy,
        )


def rows(path: Path, event: str) -> list[dict[str, Any]]:
    return [
        row
        for line in (path / "trajectory.jsonl").read_text().splitlines()
        if (row := json.loads(line))["event"] == event
    ]


@pytest.mark.parametrize("status,score", [("passed", 1), ("rejected", 0), ("invalid_run", None)])
def test_clean_stop_grades_only_confirmed_saved_bytes(
    tmp_path: Path, status: str, score: int | None
) -> None:
    runtime = Runtime()
    runtime.grade_result = {"status": status, "score": score, "reason": "synthetic"}
    client = FakeClient([response("write_test", {"content": SOURCE}), response()])
    result = execute(tmp_path, client, runtime)
    assert result["score"] == score
    assert result["status"] == ("invalid_run" if score is None else "completed")
    assert result["completion_origin"] == POLICY
    assert runtime.graded == [SOURCE.encode()] and runtime.restores == 1 and runtime.closed
    assert runtime.tools == ["write_test", "submit"]
    assert len(rows(tmp_path, "tool_result")) == 1  # private verification is not a model tool
    assert rows(tmp_path, "completion_verified")[0]["origin"] == "harness"
    assert len(client.calls) == 2


@pytest.mark.parametrize("policy", ["explicit_submit", POLICY])
def test_explicit_submit_keeps_working(tmp_path: Path, policy: str) -> None:
    runtime = Runtime()
    result = execute(
        tmp_path,
        FakeClient([response("write_test", {"content": SOURCE}), response("submit")]),
        runtime,
        policy=policy,
    )
    assert result["score"] == 1 and runtime.grades == 1


def test_default_policy_does_not_grade_saved_test_on_stop(tmp_path: Path) -> None:
    runtime = Runtime()
    result = execute(
        tmp_path,
        FakeClient([response("write_test", {"content": SOURCE}), response()]),
        runtime,
        policy="explicit_submit",
    )
    assert result["score"] == 0 and result["reason"] == "no_submission" and runtime.grades == 0


def test_prose_and_code_without_saved_write_are_never_adopted(tmp_path: Path) -> None:
    stop = response()
    stop.assistant_message["content"] = "```python\n" + SOURCE + "```"
    runtime = Runtime()
    result = execute(tmp_path, FakeClient([stop]), runtime)
    assert result["score"] == 0 and result["reason"] == "no_submission"
    assert not runtime.grades and not runtime.restores


@pytest.mark.parametrize("reason", ["length", "content_filter", "unknown", "tool_calls"])
def test_nonstop_finish_never_grades_a_saved_test(tmp_path: Path, reason: str) -> None:
    runtime = Runtime()
    result = execute(
        tmp_path,
        FakeClient(
            [response("write_test", {"content": SOURCE}), replace(response(), finish_reason=reason)]
        ),
        runtime,
    )
    assert result["score"] == 0 and runtime.grades == 0 and runtime.restores == 0


def test_provider_failure_after_save_stays_invalid_null(tmp_path: Path) -> None:
    runtime = Runtime()
    error = ProviderError("http_error", delivery_state="response_received", status_code=400)
    result = execute(
        tmp_path, FakeClient([response("write_test", {"content": SOURCE}), error]), runtime
    )
    assert result["status"] == "invalid_run" and result["score"] is None and runtime.grades == 0


@pytest.mark.parametrize("bad_read", [1, 2])
def test_cached_submission_mismatch_before_or_after_restore_is_invalid(
    tmp_path: Path, bad_read: int
) -> None:
    runtime = Runtime()
    runtime.bad_read = bad_read
    result = execute(
        tmp_path, FakeClient([response("write_test", {"content": SOURCE}), response()]), runtime
    )
    assert result["status"] == "invalid_run" and result["score"] is None and runtime.grades == 0


def test_private_submission_verification_requires_true_ack(tmp_path: Path) -> None:
    runtime = Runtime()
    runtime.bad_ack = True
    result = execute(
        tmp_path, FakeClient([response("write_test", {"content": SOURCE}), response()]), runtime
    )
    assert result["status"] == "invalid_run" and result["score"] is None and runtime.grades == 0


def test_ineligible_successful_write_is_selected_without_substitution(tmp_path: Path) -> None:
    runtime = Runtime()
    runtime.policy_allowed = False
    runtime.grade_result = {"status": "rejected", "score": 0, "reason": "policy_rejected"}
    result = execute(
        tmp_path, FakeClient([response("write_test", {"content": SOURCE}), response()]), runtime
    )
    assert result["score"] == 0 and runtime.graded == [SOURCE.encode()]


def test_latest_successful_write_wins_over_later_known_rejection(tmp_path: Path) -> None:
    runtime = Runtime()
    runtime.reject_write = "rejected"
    client = FakeClient(
        [
            response("write_test", {"content": SOURCE}),
            response("write_test", {"content": "second"}),
            response("write_test", {"content": "rejected"}),
            response(),
        ]
    )
    result = execute(tmp_path, client, runtime)
    assert result["score"] == 1 and runtime.graded == [b"second"]


def test_cleanup_failure_overrides_saved_test_grade(tmp_path: Path) -> None:
    runtime = Runtime()
    runtime.fail_close = True
    result = execute(
        tmp_path, FakeClient([response("write_test", {"content": SOURCE}), response()]), runtime
    )
    assert runtime.grades == 1 and result["status"] == "invalid_run" and result["score"] is None


@pytest.mark.parametrize("policy", ["unknown", POLICY])
def test_bad_policy_or_required_selector_rejects_before_effects(
    tmp_path: Path, policy: str
) -> None:
    runtime = Runtime()
    client = FakeClient([])
    config = model_config().model_copy(update={"tool_choice": "required"})
    with pytest.raises(ValueError):
        execute(tmp_path, client, runtime, policy=policy, config=config)
    assert not runtime.prepares and not client.calls and not runtime.grades


@pytest.mark.parametrize("event", ["completion_selected", "completion_verified", "grade_completed"])
def test_resume_reuses_stop_and_never_repeats_completed_grading(tmp_path: Path, event: str) -> None:
    first = Runtime()
    with pytest.raises(Crash):
        execute(
            tmp_path,
            FakeClient([response("write_test", {"content": SOURCE}), response()]),
            first,
            crash_event=event,
        )
    resumed = Runtime()
    client = FakeClient([])
    result = execute(tmp_path, client, resumed)
    assert result["score"] == 1 and not client.calls
    assert first.grades + resumed.grades == 1
    assert resumed.source == SOURCE.encode()
    assert len(rows(tmp_path, "completion_selected")) == 1
    assert len(rows(tmp_path, "grade_completed")) == 1
    again = Runtime()
    replay = FakeClient([])
    assert execute(tmp_path, replay, again) == result
    assert not again.prepares and not again.grades and not again.tools and not replay.calls


def test_inflight_grade_never_retries_even_with_saved_test_and_stop(tmp_path: Path) -> None:
    with pytest.raises(Crash):
        execute(
            tmp_path,
            FakeClient([response("write_test", {"content": SOURCE}), response()]),
            Runtime(),
            crash_event="grade_started",
        )
    resumed = Runtime()
    client = FakeClient([])
    result = execute(tmp_path, client, resumed)
    assert result["status"] == "invalid_run" and result["score"] is None
    assert result["reason"] == "uncertain_grade" and not resumed.grades and not client.calls


def test_policy_change_is_rejected_before_finished_result_replay(tmp_path: Path) -> None:
    execute(tmp_path, FakeClient([response()]), Runtime(), policy="explicit_submit")
    before = (tmp_path / "trajectory.jsonl").read_bytes()
    runtime = Runtime()
    client = FakeClient([])
    with pytest.raises(ValueError, match="policy"):
        execute(tmp_path, client, runtime)
    assert not runtime.prepares and not client.calls
    assert (tmp_path / "trajectory.jsonl").read_bytes() == before


def test_legacy_finished_episode_is_explicit_only_and_stays_byte_identical(tmp_path: Path) -> None:
    result = {"status": "completed", "score": 0, "reason": "no_submission"}
    with JsonlLedger(tmp_path / "trajectory.jsonl") as ledger:
        ledger.append(
            "episode_finished",
            experiment_id="experiment",
            episode_id="episode-1",
            sample_id="sample",
            result=result,
        )
    before = (tmp_path / "trajectory.jsonl").read_bytes()
    assert execute(tmp_path, FakeClient([]), Runtime(), policy="explicit_submit") == result
    with pytest.raises(ValueError, match="Legacy"):
        execute(tmp_path, FakeClient([]), Runtime())
    assert (tmp_path / "trajectory.jsonl").read_bytes() == before


def test_unacknowledged_write_is_not_selected_until_recorded_recovery_ack(tmp_path: Path) -> None:
    runtime = Runtime()
    runtime.crash_write = True
    with pytest.raises(Crash):
        execute(tmp_path, FakeClient([response("write_test", {"content": SOURCE})]), runtime)
    assert not runtime.grades and not rows(tmp_path, "completion_selected")
    recovered = Runtime()
    client = FakeClient([response()])
    result = execute(tmp_path, client, recovered)
    assert result["score"] == 1 and len(client.calls) == 1
    assert recovered.graded == [SOURCE.encode()]
    assert rows(tmp_path, "tool_recovery")


def test_unknown_effect_cannot_be_salvaged_by_clean_stop(tmp_path: Path) -> None:
    class Uncertain(Runtime):
        def tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
            result = super().tool(name, args)
            if name == "write_test":
                assert self.ledger is not None
                self.ledger.append(
                    "tool_intent",
                    experiment_id="experiment",
                    episode_id="episode-1",
                    sample_id="sample",
                    tool_id="orphan",
                    name="run_tests",
                    args={"node_ids": []},
                )
            return result

    runtime = Uncertain()
    result = execute(
        tmp_path, FakeClient([response("write_test", {"content": SOURCE}), response()]), runtime
    )
    assert result["status"] == "invalid_run" and result["score"] is None and not runtime.grades


def test_call_cap_does_not_invent_stop_but_actual_last_call_stop_qualifies(tmp_path: Path) -> None:
    path = tmp_path / "exhausted"
    runtime = Runtime()
    result = execute(
        path, FakeClient([response("write_test", {"content": SOURCE}) for _ in range(15)]), runtime
    )
    assert result["reason"] == "model_call_budget" and result["score"] == 0
    assert result["physical_calls"] == 15 and runtime.grades == 0
    path = tmp_path / "stopped"
    runtime = Runtime()
    answers: list[ProviderResponse | BaseException] = [
        response("write_test", {"content": SOURCE}) for _ in range(14)
    ]
    answers.append(response())
    result = execute(path, FakeClient(answers), runtime)
    assert result["score"] == 1 and result["physical_calls"] == 15 and runtime.grades == 1


def test_completed_grade_cannot_replay_for_another_submission_digest(tmp_path: Path) -> None:
    with pytest.raises(Crash):
        execute(
            tmp_path,
            FakeClient([response("write_test", {"content": SOURCE}), response()]),
            Runtime(),
            crash_event="grade_completed",
        )
    path = tmp_path / "trajectory.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines()]
    for event in events:
        if event["event"] == "grade_started":
            event["submission_sha256"] = "0" * 64
    path.write_text("".join(json.dumps(row) + "\n" for row in events))
    runtime = Runtime()
    client = FakeClient([])
    result = execute(tmp_path, client, runtime)
    assert result["status"] == "invalid_run" and result["score"] is None
    assert not runtime.grades and not client.calls


def test_model_response_text_never_replaces_successful_saved_file(tmp_path: Path) -> None:
    stop = response()
    stop.assistant_message["content"] = "```python\nassert False\n```"
    runtime = Runtime()
    result = execute(
        tmp_path, FakeClient([response("write_test", {"content": SOURCE}), stop]), runtime
    )
    assert result["score"] == 1 and runtime.graded == [SOURCE.encode()]


@pytest.mark.parametrize("source", ["", "x" * 65537])
def test_empty_or_oversized_source_has_honest_saved_state(tmp_path: Path, source: str) -> None:
    runtime = Runtime()
    runtime.grade_result = {"status": "rejected", "score": 0, "reason": "policy_rejected"}
    result = execute(
        tmp_path, FakeClient([response("write_test", {"content": source}), response()]), runtime
    )
    assert result["score"] == 0
    assert runtime.graded == ([b""] if not source else [])


def test_budget_exhaustion_after_saved_write_does_not_finalize(tmp_path: Path) -> None:
    runtime = Runtime()
    client = FakeClient([response("write_test", {"content": SOURCE}), response()])
    result = execute(tmp_path, client, runtime, budget_usd=Decimal("0.00984"))
    assert result["status"] == "incomplete" and result["reason"] == "budget_limit"
    assert result["score"] is None and runtime.source == SOURCE.encode()
    assert len(client.calls) == 1 and not runtime.grades


def test_malformed_empty_tool_container_does_not_qualify_as_stop(tmp_path: Path) -> None:
    stop = response()
    stop.assistant_message["tool_calls"] = {}
    runtime = Runtime()
    result = execute(
        tmp_path, FakeClient([response("write_test", {"content": SOURCE}), stop]), runtime
    )
    assert result["score"] == 0 and not runtime.grades


def test_truthy_write_ack_is_not_a_confirmed_save(tmp_path: Path) -> None:
    class Truthy(Runtime):
        def tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
            reply = super().tool(name, args)
            if name == "write_test":
                reply["ok"] = 1
            return reply

    runtime = Truthy()
    result = execute(
        tmp_path, FakeClient([response("write_test", {"content": SOURCE}), response()]), runtime
    )
    assert result["score"] == 0 and not runtime.grades


@pytest.mark.docker
@pytest.mark.parametrize("case_prefix", ["more-itertools-71b768", "more-itertools-2b91"])
def test_saved_stop_uses_unchanged_oldest_and_latest_pinned_grader(
    tmp_path: Path, case_prefix: str
) -> None:
    from revertbench.harness.runtime import PinnedRuntime

    cases = Path(__file__).resolve().parents[1] / "cases"
    case_dir = next(path for path in cases.iterdir() if path.name.startswith(case_prefix))
    # Public maintainer reference: trusted fake-response fixture, with no live model call.
    source = (case_dir / "reference.py").read_text()
    client = FakeClient(
        [
            response("write_test", {"content": source}),
            response("run_tests", {"node_ids": []}),
            response(),
        ]
    )
    runtime = PinnedRuntime(
        case_dir, cache=tmp_path / "engines", output=tmp_path / "runtime", episode_id="episode-1"
    )
    with (
        JsonlLedger(tmp_path / "project.jsonl") as project,
        JsonlLedger(tmp_path / "trajectory.jsonl") as ledger,
    ):
        result = run_episode(
            identity=EpisodeIdentity("experiment", "episode-1", "sample"),
            config=model_config(),
            client=client,
            runtime=runtime,
            ledger=ledger,
            budget=CostBudget(project, "experiment", Decimal("0.25")),
            scheduler=RateScheduler(
                project, "model", rpm=1000, tpm=10000000, rpd=10000, tpd=100000000
            ),
            completion_policy="saved_test_on_stop_v1",
            clock=lambda: 1000.0,
        )
    assert result["status"] == "completed" and result["score"] == 1
    assert result["completion_origin"] == POLICY and result["preview_invoked"] is True
    assert len(result["grade"]["executions"]) == 6 and len(client.calls) == 3
    assert [row["name"] for row in rows(tmp_path, "tool_result")] == ["write_test", "run_tests"]
    assert rows(tmp_path, "completion_verified")[0]["origin"] == "harness"
    metadata = result["runtime_metadata"]
    case = json.loads((case_dir / "case.json").read_bytes())
    assert metadata["grader_sha256"] == case["validation"]["grader_sha256"]
    assert metadata["dependency_lock_sha256"] == case["env"]["dependency_lock_sha256"]
