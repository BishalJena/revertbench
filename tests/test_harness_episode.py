import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from revertbench.harness.episode import EpisodeIdentity, run_episode
from revertbench.models.budget import CostBudget
from revertbench.models.client import PreparedRequest, ProviderError, ProviderResponse, TokenUsage
from revertbench.models.config import ModelConfig
from revertbench.models.ledger import JsonlLedger
from revertbench.models.scheduler import RateScheduler


def model_config() -> ModelConfig:
    return ModelConfig.model_validate(
        {
            "alias": "test",
            "provider": {
                "id": "groq",
                "base_url": "https://api.groq.com/openai/v1",
                "api_key_env": "UNUSED_TEST_KEY",
            },
            "model_id": "openai/gpt-oss-20b",
            "model_context_tokens": 131072,
            "max_completion_tokens": 32,
            "pricing": {
                "input_usd_per_million": "0.075",
                "output_usd_per_million": "0.30",
                "source_url": "https://example.com/prices",
                "observed_at": datetime(2026, 11, 21, tzinfo=UTC),
            },
            "quotas": {
                "rpm": 1000,
                "tpm": 10000000,
                "rpd": 10000,
                "tpd": 100000000,
                "evidence": "conservative",
                "source_url": "https://example.com/quotas",
                "observed_at": datetime(2026, 11, 21, tzinfo=UTC),
            },
        }
    )


def response(
    name: str | None = None, args: dict[str, Any] | None = None, *, multiple: bool = False
) -> ProviderResponse:
    message: dict[str, Any] = {"role": "assistant", "content": None, "reasoning": "toy reasoning"}
    if name:
        call = {
            "id": "call",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args or {})},
        }
        message["tool_calls"] = [call, call] if multiple else [call]
    raw = json.dumps({"choices": [{"message": message}]}).encode()
    return ProviderResponse(
        200,
        raw,
        {"choices": []},
        message,
        TokenUsage(10, 2, 12, {}),
        "tool_calls" if name else "stop",
        "openai/gpt-oss-20b",
        "response-id",
        None,
        {},
        0.1,
    )


class Crash(BaseException):
    pass


class FakeClient:
    def __init__(self, answers: list[ProviderResponse | BaseException]) -> None:
        self.answers = answers
        self.calls: list[PreparedRequest] = []

    def complete(self, prepared: PreparedRequest) -> ProviderResponse:
        self.calls.append(prepared)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class FakeRuntime:
    def __init__(self) -> None:
        self.tools: list[str] = []
        self.source: bytes | None = None
        self.prepares = 0
        self.grades = 0
        self.restores = 0
        self.closed = False
        self.crash_write = False
        self.fail_close = False
        self.grade_result: dict[str, Any] = {"status": "passed", "score": 1, "reason": "passed"}
        self.ledger: JsonlLedger | None = None

    def prepare(self) -> dict[str, Any]:
        self.prepares += 1
        return {
            "safe_task": {
                "episode_id": "episode-1",
                "bad_pr_title": "Buggy change",
                "bad_diff": "safe diff",
            },
            "case_id": "PRIVATE_FIX_SHA",
            "reference": "PRIVATE_REFERENCE",
            "repair": "PRIVATE_REPAIR",
        }

    def tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        assert self.ledger is not None
        assert any(row["event"] == "model_completed" for row in self.ledger.events)
        self.tools.append(name)
        if name == "write_test":
            assert any(row["event"] == "submission_intended" for row in self.ledger.events)
            self.source = args["content"].encode()
            if self.crash_write:
                raise Crash()
            return {"ok": True, "bytes": len(self.source)}
        if name == "submit":
            return {"submitted": True}
        return {"ok": True}

    def restore_submission(self, source: bytes) -> None:
        self.source = source
        self.restores += 1

    def get_submission(self) -> bytes:
        if self.source is None:
            raise RuntimeError("no_submission")
        return self.source

    def grade(self) -> dict[str, Any]:
        self.grades += 1
        return self.grade_result

    def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("cleanup_failed")


def execute(
    tmp_path: Path, client: FakeClient, runtime: FakeRuntime, *, sample: str = "sample-1"
) -> dict[str, Any]:
    from decimal import Decimal

    config = model_config()
    with (
        JsonlLedger(tmp_path / "project-ledger.jsonl") as project,
        JsonlLedger(tmp_path / "trajectory.jsonl") as ledger,
    ):
        runtime.ledger = ledger
        return run_episode(
            identity=EpisodeIdentity("experiment", "episode-1", sample),
            config=config,
            client=client,
            runtime=runtime,
            ledger=ledger,
            budget=CostBudget(project, "experiment", Decimal("0.25")),
            scheduler=RateScheduler(
                project, "groq:openai/gpt-oss-20b", rpm=1000, tpm=10000000, rpd=10000, tpd=100000000
            ),
            clock=lambda: 1000.0,
        )


def test_completed_sample_replays_without_new_provider_tools_or_grade(tmp_path: Path) -> None:
    client = FakeClient(
        [
            response("write_test", {"content": "def test_ok():\n    assert True\n"}),
            response("submit"),
        ]
    )
    runtime = FakeRuntime()
    result = execute(tmp_path, client, runtime)
    assert result["status"] == "completed" and result["score"] == 1
    assert runtime.grades == 1 and runtime.closed
    wire = b"\n".join(request.body for request in client.calls)
    assert all(
        secret not in wire
        for secret in (b"PRIVATE_FIX_SHA", b"PRIVATE_REFERENCE", b"PRIVATE_REPAIR")
    )
    replay_client = FakeClient([])
    replay_runtime = FakeRuntime()
    assert execute(tmp_path, replay_client, replay_runtime) == result
    assert not replay_client.calls and not replay_runtime.tools and not replay_runtime.grades


def test_independent_sample_never_uses_another_samples_response(tmp_path: Path) -> None:
    first, second = FakeClient([response()]), FakeClient([response()])
    execute(tmp_path, first, FakeRuntime(), sample="one")
    execute(tmp_path, second, FakeRuntime(), sample="two")
    assert len(first.calls) == len(second.calls) == 1
    assert first.calls[0].body == second.calls[0].body


def test_started_without_response_is_uncertain_and_never_auto_retried(tmp_path: Path) -> None:
    with pytest.raises(Crash):
        execute(tmp_path, FakeClient([Crash()]), FakeRuntime())
    client = FakeClient([])
    result = execute(tmp_path, client, FakeRuntime())
    assert result["status"] == "invalid_run" and result["score"] is None
    assert result["reason"] == "uncertain_model_call" and not client.calls


def test_completed_response_precedes_write_and_pending_write_recovers_idempotently(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    runtime.crash_write = True
    with pytest.raises(Crash):
        execute(tmp_path, FakeClient([response("write_test", {"content": "assert True"})]), runtime)
    resumed = FakeRuntime()
    client = FakeClient([response("submit")])
    result = execute(tmp_path, client, resumed)
    assert result["score"] == 1 and resumed.restores == 1 and len(client.calls) == 1
    assert resumed.source == b"assert True"


def test_multiple_tools_execute_nothing_and_are_model_failure(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    result = execute(
        tmp_path, FakeClient([response("list_dir", {"path": "."}, multiple=True)]), runtime
    )
    assert result["status"] == "completed" and result["score"] == 0 and not runtime.tools


def test_provider_fault_is_invalid_null_not_model_failure(tmp_path: Path) -> None:
    client = FakeClient([ProviderError("transport_error", delivery_state="possibly_sent")])
    result = execute(tmp_path, client, FakeRuntime())
    assert result["status"] == "invalid_run" and result["score"] is None


def test_cleanup_failure_cannot_leave_a_passing_episode(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    runtime.fail_close = True
    result = execute(
        tmp_path,
        FakeClient([response("write_test", {"content": "assert True"}), response("submit")]),
        runtime,
    )
    assert result["status"] == "invalid_run" and result["score"] is None
    assert result["reason"] == "runtime_cleanup_failed"


def test_invalid_tool_shape_does_not_execute_or_commit_submission(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    result = execute(
        tmp_path,
        FakeClient([response("write_test", {"content": "assert True", "extra": True}), response()]),
        runtime,
    )
    assert result["status"] == "completed" and result["score"] == 0
    assert not runtime.tools and runtime.source is None


def test_non_string_tool_name_is_model_protocol_failure_not_host_crash(tmp_path: Path) -> None:
    from dataclasses import replace

    original = response("submit")
    message: dict[str, Any] = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call", "function": {"name": [], "arguments": "{}"}}],
    }
    runtime = FakeRuntime()
    result = execute(tmp_path, FakeClient([replace(original, assistant_message=message)]), runtime)
    assert result["status"] == "completed" and result["score"] == 0 and not runtime.tools


def test_invalid_unicode_submission_is_not_infrastructure_failure(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    result = execute(tmp_path, FakeClient([response("write_test", {"content": "\ud800"})]), runtime)
    assert result["status"] == "completed" and result["score"] == 0 and not runtime.tools


def test_oversize_write_reply_is_durable_and_never_commits_t(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    result = execute(
        tmp_path,
        FakeClient([response("write_test", {"content": "x" * 65537}), response()]),
        runtime,
    )
    assert result["score"] == 0 and not runtime.tools
    with JsonlLedger(tmp_path / "trajectory.jsonl") as ledger:
        replies = [row for row in ledger.events if row["event"] == "tool_result"]
        assert replies[0]["response"] == {"ok": False, "error": "submission_too_large"}
        assert not any(row["event"] == "submission_intended" for row in ledger.events)


def test_tool_type_and_duplicate_json_arguments_cannot_execute(tmp_path: Path) -> None:
    from dataclasses import replace

    for index, call in enumerate(
        [
            {
                "id": "call",
                "type": "other",
                "function": {"name": "list_dir", "arguments": '{"path":"."}'},
            },
            {
                "id": "call",
                "type": "function",
                "function": {
                    "name": "write_test",
                    "arguments": '{"content":"first","content":"second"}',
                },
            },
        ]
    ):
        message: dict[str, Any] = {"role": "assistant", "content": None, "tool_calls": [call]}
        runtime = FakeRuntime()
        execute(
            tmp_path / str(index),
            FakeClient([replace(response(), assistant_message=message), response()]),
            runtime,
        )
        assert not runtime.tools


def test_unexecuted_run_tests_error_is_not_reported_as_a_test_run(tmp_path: Path) -> None:
    class NoSubmission(FakeRuntime):
        def tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
            self.tools.append(name)
            return {"ok": False, "error": "no_submission"}

    result = execute(
        tmp_path, FakeClient([response("run_tests", {"node_ids": []}), response()]), NoSubmission()
    )
    assert result["preview_invoked"] is False


def test_success_result_includes_actual_private_runtime_environment(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    result = execute(tmp_path, FakeClient([response()]), runtime)
    assert result["runtime_metadata"]["case_id"] == "PRIVATE_FIX_SHA"


def test_rejected_write_does_not_replace_last_successful_submission(tmp_path: Path) -> None:
    class RejectSecond(FakeRuntime):
        def tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
            if name == "write_test" and self.source is not None:
                return {"ok": False, "error": "write_rejected"}
            return super().tool(name, args)

    runtime = RejectSecond()
    result = execute(
        tmp_path,
        FakeClient(
            [
                response("write_test", {"content": "first"}),
                response("write_test", {"content": "second"}),
                response("submit"),
            ]
        ),
        runtime,
    )
    assert result["score"] == 1 and runtime.source == b"first"


def test_physical_request_cap_is_fifteen_and_previews_are_not_required(tmp_path: Path) -> None:
    client = FakeClient([response("list_dir", {"path": "."}) for _ in range(15)])
    result = execute(tmp_path, client, FakeRuntime())
    assert len(client.calls) == result["physical_calls"] == 15
    assert result["status"] == "completed" and result["score"] == 0
    assert result["reason"] == "model_call_budget" and result["preview_invoked"] is False


def test_429_retry_is_explicit_and_retains_unknown_first_reservation(tmp_path: Path) -> None:
    client = FakeClient(
        [
            ProviderError(
                "rate_limited",
                delivery_state="response_received",
                status_code=429,
                safe_headers={"retry-after": "0"},
            ),
            response(),
        ]
    )
    result = execute(tmp_path, client, FakeRuntime())
    assert len(client.calls) == result["physical_calls"] == 2
    with JsonlLedger(tmp_path / "trajectory.jsonl") as ledger:
        assert len([row for row in ledger.events if row["event"] == "model_retry"]) == 1
    with JsonlLedger(tmp_path / "project-ledger.jsonl") as project:
        from decimal import Decimal

        assert CostBudget(project, "experiment", Decimal("0.25")).totals()["reserved"] > 0


def test_unresolved_pytest_does_not_run_again_after_resume(tmp_path: Path) -> None:
    class CrashPreview(FakeRuntime):
        def tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
            if name == "run_tests":
                raise Crash()
            return super().tool(name, args)

    with pytest.raises(Crash):
        execute(
            tmp_path,
            FakeClient(
                [
                    response("write_test", {"content": "assert True"}),
                    response("run_tests", {"node_ids": []}),
                ]
            ),
            CrashPreview(),
        )
    client = FakeClient([])
    runtime = FakeRuntime()
    result = execute(tmp_path, client, runtime)
    assert result["status"] == "invalid_run" and result["reason"] == "uncertain_test_tool"
    assert not client.calls and not runtime.tools


def test_unresolved_grade_does_not_run_again_after_resume(tmp_path: Path) -> None:
    class CrashGrade(FakeRuntime):
        def grade(self) -> dict[str, Any]:
            raise Crash()

    with pytest.raises(Crash):
        execute(
            tmp_path,
            FakeClient([response("write_test", {"content": "assert True"}), response("submit")]),
            CrashGrade(),
        )
    client = FakeClient([])
    runtime = FakeRuntime()
    result = execute(tmp_path, client, runtime)
    assert result["status"] == "invalid_run" and result["reason"] == "uncertain_grade"
    assert not client.calls and not runtime.grades


def test_completed_grade_replays_when_final_checkpoint_was_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = JsonlLedger.append

    def interrupted(self: JsonlLedger, event: str, **data: Any) -> dict[str, Any]:
        if event == "episode_finished":
            raise Crash()
        return original(self, event, **data)

    monkeypatch.setattr(JsonlLedger, "append", interrupted)
    with pytest.raises(Crash):
        execute(
            tmp_path,
            FakeClient([response("write_test", {"content": "assert True"}), response("submit")]),
            FakeRuntime(),
        )
    monkeypatch.setattr(JsonlLedger, "append", original)
    client = FakeClient([])
    runtime = FakeRuntime()
    result = execute(tmp_path, client, runtime)
    assert result["score"] == 1 and not client.calls and not runtime.grades


def test_financial_reservation_blocks_before_any_provider_attempt(tmp_path: Path) -> None:
    from decimal import Decimal

    client = FakeClient([])
    runtime = FakeRuntime()
    config = model_config()
    with (
        JsonlLedger(tmp_path / "budget.jsonl") as project,
        JsonlLedger(tmp_path / "calls.jsonl") as ledger,
    ):
        runtime.ledger = ledger
        result = run_episode(
            identity=EpisodeIdentity("experiment", "episode-1", "one"),
            config=config,
            client=client,
            runtime=runtime,
            ledger=ledger,
            budget=CostBudget(project, "experiment", Decimal("0.001")),
            scheduler=RateScheduler(
                project, "model", rpm=1000, tpm=1000000, rpd=10000, tpd=10000000
            ),
        )
    assert (
        result["status"] == "incomplete"
        and result["reason"] == "budget_limit"
        and result["score"] is None
    )
    assert not client.calls and result["physical_calls"] == 0


def test_known_not_sent_errors_are_attempts_without_claiming_network_or_unknown_usage(
    tmp_path: Path,
) -> None:
    errors: list[ProviderResponse | BaseException] = [
        ProviderError("missing_credential", delivery_state="not_sent") for _ in range(2)
    ]
    result = execute(tmp_path, FakeClient(errors), FakeRuntime())
    assert result["request_attempts"] == 2 and result["physical_calls"] == 0
    assert result["not_sent_attempts"] == 2 and result["unknown_usage_attempts"] == 0
