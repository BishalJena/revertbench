"""Sequential bad-only episodes with durable model, tool and grading boundaries."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Protocol, cast

from revertbench.harness.prompt import MAX_MODEL_CALLS, TOOL_DEFINITIONS, TaskPrompt, build_messages
from revertbench.models.budget import BudgetExceeded, CostBudget
from revertbench.models.client import (
    JsonValue,
    PreparedRequest,
    ProviderError,
    ProviderResponse,
    prepare_request,
)
from revertbench.models.config import ModelConfig
from revertbench.models.ledger import JsonlLedger, LedgerError, request_identity
from revertbench.models.scheduler import QuotaExceeded, RateScheduler


@dataclass(frozen=True)
class EpisodeIdentity:
    experiment_id: str
    episode_id: str
    sample_id: str


class CompletionClient(Protocol):
    def complete(self, request: PreparedRequest) -> ProviderResponse: ...


class EpisodeRuntime(Protocol):
    def prepare(self) -> dict[str, Any]: ...
    def tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]: ...
    def restore_submission(self, source: bytes) -> None: ...
    def get_submission(self) -> bytes: ...
    def grade(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


class _Halt(RuntimeError):
    def __init__(self, status: str, reason: str) -> None:
        self.status, self.reason = status, reason
        super().__init__(reason)


def _response_data(response: ProviderResponse) -> dict[str, Any]:
    return {
        "raw_body_b64": base64.b64encode(response.raw_body).decode(),
        "assistant_message": response.assistant_message,
        "usage": asdict(response.usage) if response.usage else None,
        "finish_reason": response.finish_reason,
        "model": response.model,
        "response_id": response.response_id,
        "system_fingerprint": response.system_fingerprint,
        "safe_headers": response.safe_headers,
        "elapsed_seconds": response.elapsed_seconds,
    }


def _valid_arguments(name: str, args: Any) -> bool:
    definition = next(
        tool["function"]["parameters"]
        for tool in TOOL_DEFINITIONS
        if tool["function"]["name"] == name
    )
    properties = definition["properties"]
    if not isinstance(args, dict) or set(args) != set(properties):
        return False
    for key, schema in properties.items():
        value = args[key]
        if schema["type"] == "string" and not isinstance(value, str):
            return False
        if schema["type"] == "integer" and (
            type(value) is not int or value < schema.get("minimum", 0)
        ):
            return False
        if schema["type"] == "array" and (
            not isinstance(value, list) or not all(isinstance(item, str) for item in value)
        ):
            return False
    return True


def _unique_arguments(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate tool argument")
        result[key] = value
    return result


def _preview_invoked(reply: dict[str, Any]) -> bool:
    return (
        reply.get("status") in {"completed", "timeout", "output_limit"}
        and "exit_code" in reply
        and (reply["exit_code"] is None or type(reply["exit_code"]) is int)
    )


@dataclass
class _Session:
    identity: EpisodeIdentity
    config: ModelConfig
    client: CompletionClient
    runtime: EpisodeRuntime
    ledger: JsonlLedger
    budget: CostBudget
    scheduler: RateScheduler
    clock: Callable[[], float]
    sleep: Callable[[float], None]

    def rows(self, event: str) -> list[dict[str, Any]]:
        return [
            row
            for row in self.ledger.events
            if row["event"] == event
            and all(row.get(key) == value for key, value in asdict(self.identity).items())
        ]

    def append(self, event: str, **data: Any) -> dict[str, Any]:
        return self.ledger.append(event, **asdict(self.identity), **data)

    def result(
        self, status: str, reason: str, score: int | None = None, **data: Any
    ) -> dict[str, Any]:
        completed = self.rows("model_completed")
        usages = [
            row["response"]["usage"] for row in completed if row["response"]["usage"] is not None
        ]
        not_sent = {
            row["call_id"]
            for row in self.rows("model_error")
            if row["delivery_state"] == "not_sent"
        }
        return {
            **asdict(self.identity),
            "status": status,
            "reason": reason,
            "score": score,
            "request_attempts": len(self.rows("model_started")),
            "not_sent_attempts": len(not_sent),
            "physical_calls": len(self.rows("model_started")) - len(not_sent),
            "replayed_calls": len(self.rows("model_replayed")),
            "prompt_tokens": sum(usage["prompt_tokens"] for usage in usages),
            "completion_tokens": sum(usage["completion_tokens"] for usage in usages),
            "unknown_usage_attempts": len(self.rows("model_started")) - len(usages) - len(not_sent),
            "preview_invoked": any(
                row.get("name") == "run_tests" and _preview_invoked(row["response"])
                for row in self.rows("tool_result")
            ),
            "runtime_metadata": self.rows("episode_prepared")[-1]["metadata"]
            if self.rows("episode_prepared")
            else None,
            **data,
        }

    def submission(self) -> dict[str, Any] | None:
        replies = {
            row["tool_id"]: row["response"]
            for row in self.rows("tool_result")
            if row["name"] == "write_test"
        }
        return next(
            (
                row
                for row in reversed(self.rows("submission_intended"))
                if row["tool_id"] not in replies or replies[row["tool_id"]].get("ok") is True
            ),
            None,
        )

    def _settle(self, call_id: str, response: dict[str, Any]) -> None:
        usage = response["usage"]
        actual = None
        if usage is not None:
            actual = (
                Decimal(usage["prompt_tokens"]) * self.config.pricing.input_usd_per_million
                + Decimal(usage["completion_tokens"]) * self.config.pricing.output_usd_per_million
            ) / Decimal(1_000_000)
            self.scheduler.record_usage(call_id, usage["total_tokens"])
        self.budget.settle(call_id, actual)

    def _wait(self, tokens: int) -> None:
        delay = self.scheduler.delay(tokens, now=self.clock())
        if delay > 60:
            self.append("quota_wait", retry_after_seconds=delay)
            raise _Halt("incomplete", "quota_wait")
        if delay > 0:
            self.append("quota_wait", retry_after_seconds=delay)
            self.sleep(delay)
        if self.scheduler.delay(tokens, now=self.clock()) > 0:
            raise _Halt("incomplete", "quota_wait")

    def response(self, messages: list[dict[str, Any]], turn: int) -> dict[str, Any]:
        prepared = prepare_request(
            self.config,
            messages=cast(list[dict[str, JsonValue]], messages),
            tools=cast(list[dict[str, JsonValue]], TOOL_DEFINITIONS),
        )
        request_hash = request_identity(prepared.body, {"config_sha256": prepared.config_sha256})
        ceiling = (
            Decimal(self.config.model_context_tokens) * self.config.pricing.input_usd_per_million
            + Decimal(self.config.max_completion_tokens)
            * self.config.pricing.output_usd_per_million
        ) / Decimal(1_000_000)
        for attempt in range(2):
            call_id = "/".join(
                (
                    self.identity.experiment_id,
                    self.identity.episode_id,
                    self.identity.sample_id,
                    str(turn),
                    str(attempt),
                )
            )
            cached = self.ledger.model_response(call_id, request_hash)
            if cached is not None:
                self.budget.reserve(call_id, ceiling)
                self._settle(call_id, cached)
                self.append("model_replayed", call_id=call_id, request_hash=request_hash)
                return cached
            errors = [row for row in self.rows("model_error") if row["call_id"] == call_id]
            if errors:
                if attempt == 0 and (
                    errors[-1]["delivery_state"] == "not_sent" or errors[-1]["status_code"] == 429
                ):
                    continue
                raise _Halt("invalid_run", "provider_" + errors[-1]["category"])
            if self.ledger.started(call_id):
                raise _Halt("invalid_run", "uncertain_model_call")
            if len(self.rows("model_started")) >= MAX_MODEL_CALLS:
                raise _Halt(
                    "invalid_run" if self.rows("model_error") else "completed", "model_call_budget"
                )
            tokens = len(prepared.body) + self.config.max_completion_tokens
            self._wait(tokens)
            self.budget.reserve(call_id, ceiling)
            self.append(
                "model_intent",
                call_id=call_id,
                turn_id=turn,
                attempt_id=attempt,
                request_hash=request_hash,
                config_sha256=prepared.config_sha256,
                wire_body_b64=base64.b64encode(prepared.body).decode(),
            )
            self.append("model_started", call_id=call_id, request_hash=request_hash)
            self.scheduler.record_start(call_id, tokens, now=self.clock())
            try:
                reply = self.client.complete(prepared)
            except ProviderError as error:
                self.append(
                    "model_error",
                    call_id=call_id,
                    request_hash=request_hash,
                    category=error.category,
                    status_code=error.status_code,
                    delivery_state=error.delivery_state,
                    safe_headers=error.safe_headers,
                    raw_body_b64=base64.b64encode(error.raw_body).decode()
                    if error.raw_body
                    else None,
                )
                self.budget.settle(
                    call_id, Decimal(0) if error.delivery_state == "not_sent" else None
                )
                if error.status_code == 429:
                    try:
                        seconds = float(error.safe_headers.get("retry-after", "60"))
                    except ValueError:
                        seconds = 60.0
                    self.scheduler.cooldown(min(max(seconds, 0), 86400), now=self.clock())
                if attempt == 0 and (
                    error.delivery_state == "not_sent" or error.status_code == 429
                ):
                    self.append("model_retry", retry_of=call_id, next_attempt_id=1)
                    continue
                raise _Halt("invalid_run", "provider_" + error.category) from None
            response = _response_data(reply)
            self.append(
                "model_completed", call_id=call_id, request_hash=request_hash, response=response
            )
            self._settle(call_id, response)
            return response
        raise _Halt("invalid_run", "provider_attempts_exhausted")

    def tool(self, call: dict[str, Any], turn: int) -> tuple[str, dict[str, Any]]:
        identifier = call.get("id")
        function = call.get("function")
        if (
            call.get("type") != "function"
            or not isinstance(identifier, str)
            or not 1 <= len(identifier) <= 256
            or not isinstance(function, dict)
        ):
            raise _Halt("completed", "invalid_tool_request")
        name = function.get("name")
        arguments = function.get("arguments")
        allowed = {tool["function"]["name"] for tool in TOOL_DEFINITIONS}
        if not isinstance(name, str) or name not in allowed or not isinstance(arguments, str):
            raise _Halt("completed", "invalid_tool_request")
        try:
            if len(arguments.encode()) > 262144:
                raise _Halt("completed", "invalid_tool_request")
        except UnicodeError:
            raise _Halt("completed", "invalid_tool_arguments") from None
        key = f"{turn}:{identifier}"
        done = [row for row in self.rows("tool_result") if row["tool_id"] == key]
        if done:
            self.append("tool_replayed", tool_id=key)
            return identifier, done[-1]["response"]
        try:
            args = json.loads(arguments, object_pairs_hook=_unique_arguments)
        except (ValueError, RecursionError):
            args = None
        if not _valid_arguments(name, args):
            reply = {"ok": False, "error": "invalid_tool_arguments"}
            self.append("tool_result", tool_id=key, name=name, response=reply, executed=False)
            return identifier, reply
        pending = [row for row in self.rows("tool_intent") if row["tool_id"] == key]
        if pending and name == "run_tests":
            raise _Halt("invalid_run", "uncertain_test_tool")
        if pending:
            self.append("tool_recovery", tool_id=key, name=name)
        else:
            self.append("tool_intent", tool_id=key, name=name, args=args)
        if name == "write_test" and isinstance(args.get("content"), str):
            try:
                source = args["content"].encode("utf-8")
            except UnicodeError:
                raise _Halt("completed", "invalid_tool_arguments") from None
            if len(source) > 65536:
                reply = {"ok": False, "error": "submission_too_large"}
                self.append("tool_result", tool_id=key, name=name, response=reply, executed=False)
                return identifier, reply
            self.append(
                "submission_intended",
                tool_id=key,
                source_b64=base64.b64encode(source).decode(),
                sha256=hashlib.sha256(source).hexdigest(),
            )
        reply = self.runtime.tool(name, args)
        self.append("tool_result", tool_id=key, name=name, response=reply)
        return identifier, reply

    def drive(self) -> dict[str, Any]:
        metadata = self.runtime.prepare()
        self.append("episode_prepared", metadata=metadata)
        safe = metadata.get("safe_task")
        if (
            not isinstance(safe, dict)
            or safe.get("episode_id") != self.identity.episode_id
            or not all(isinstance(safe.get(key), str) for key in ("bad_pr_title", "bad_diff"))
        ):
            raise RuntimeError("runtime task identity mismatch")
        task = TaskPrompt(self.identity.episode_id, safe["bad_pr_title"], safe["bad_diff"])
        messages = build_messages(task)
        intended = self.submission()
        if intended is not None:
            source = base64.b64decode(intended["source_b64"], validate=True)
            if hashlib.sha256(source).hexdigest() != intended["sha256"]:
                raise LedgerError("Submission checkpoint identity changed")
            self.runtime.restore_submission(source)
            self.append("submission_restored", sha256=intended["sha256"])
        for turn in range(MAX_MODEL_CALLS):
            response = self.response(messages, turn)
            message = response["assistant_message"]
            messages.append(message)
            calls = message.get("tool_calls")
            if not calls:
                return self.result("completed", "no_submission", 0)
            if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
                return self.result("completed", "multiple_or_invalid_tools", 0)
            identifier, reply = self.tool(calls[0], turn)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": identifier,
                    "content": json.dumps(reply, sort_keys=True),
                }
            )
            if calls[0]["function"]["name"] == "submit":
                if reply.get("submitted") is not True:
                    return self.result("completed", "no_submission", 0)
                return self.grade()
        return self.result("completed", "model_call_budget", 0)

    def grade(self) -> dict[str, Any]:
        source = self.runtime.get_submission()
        intended = self.submission()
        if intended is None or hashlib.sha256(source).hexdigest() != intended["sha256"]:
            raise RuntimeError("Submitted bytes differ from intended submission")
        completed = self.rows("grade_completed")
        if completed:
            grade = completed[-1]["grade"]
        else:
            if self.rows("grade_started"):
                raise _Halt("invalid_run", "uncertain_grade")
            self.append("grade_started", submission_sha256=hashlib.sha256(source).hexdigest())
            grade = self.runtime.grade()
            self.append("grade_completed", grade=grade)
        status, score = grade.get("status"), grade.get("score")
        if status == "passed" and type(score) is int and score == 1:
            return self.result("completed", grade.get("reason", "passed"), 1, grade=grade)
        if status == "rejected" and type(score) is int and score == 0:
            return self.result("completed", grade.get("reason", "rejected"), 0, grade=grade)
        return self.result("invalid_run", grade.get("reason", "invalid_grade"), grade=grade)


def run_episode(
    *,
    identity: EpisodeIdentity,
    config: ModelConfig,
    client: CompletionClient,
    runtime: EpisodeRuntime,
    ledger: JsonlLedger,
    budget: CostBudget,
    scheduler: RateScheduler,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    session = _Session(identity, config, client, runtime, ledger, budget, scheduler, clock, sleep)
    finished = session.rows("episode_finished")
    if finished:
        return cast(dict[str, Any], finished[-1]["result"])
    result: dict[str, Any] | None = None
    try:
        result = session.drive()
    except _Halt as error:
        result = session.result(
            error.status, error.reason, 0 if error.status == "completed" else None
        )
    except (BudgetExceeded, QuotaExceeded) as error:
        result = session.result(
            "incomplete",
            "budget_limit"
            if isinstance(error, BudgetExceeded)
            else "request_exceeds_operational_quota",
        )
    except (RuntimeError, OSError, ValueError):
        result = session.result("invalid_run", "runtime_or_evidence_failure")
    finally:
        try:
            runtime.close()
        except (RuntimeError, OSError):
            result = session.result("invalid_run", "runtime_cleanup_failed")
    assert result is not None
    session.append(
        "episode_paused" if result["status"] == "incomplete" else "episode_finished", result=result
    )
    return result
