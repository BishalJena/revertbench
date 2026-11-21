"""One-attempt host-side compatible chat transport; never executes model tools."""

import hashlib
import http.client
import json
import os
import time
from dataclasses import dataclass, field
from email.message import Message
from typing import Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from revertbench.models.config import ModelConfig

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
DeliveryState = Literal["not_sent", "possibly_sent", "response_received"]
CLIENT_USER_AGENT = "RevertBench/0.0.1"
_RESPONSE_LIMIT = 2097152
_SAFE_HEADERS = frozenset(
    {
        "retry-after",
        *(
            f"x-ratelimit-{a}-{b}"
            for a in ("limit", "remaining", "reset")
            for b in ("requests", "tokens")
        ),
    }
)


@dataclass(frozen=True)
class PreparedRequest:
    body: bytes = field(repr=False)
    request_sha256: str
    config_sha256: str


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    raw_body: bytes = field(repr=False)
    safe_headers: dict[str, str] = field(default_factory=dict, repr=False)


class HttpTransport(Protocol):
    def __call__(
        self, request: Request, *, timeout_seconds: float, response_limit_bytes: int
    ) -> HttpResponse: ...


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    raw: dict[str, JsonValue] = field(repr=False)


@dataclass(frozen=True)
class ProviderResponse:
    status_code: int
    raw_body: bytes = field(repr=False)
    payload: dict[str, JsonValue] = field(repr=False)
    assistant_message: dict[str, JsonValue] = field(repr=False)
    usage: TokenUsage | None
    finish_reason: str
    model: str
    response_id: str | None
    system_fingerprint: str | None
    safe_headers: dict[str, str] = field(repr=False)
    elapsed_seconds: float


class ProviderError(ValueError):
    """Safe public error; bounded raw evidence is available without being repr'd."""

    def __init__(
        self,
        category: str,
        *,
        delivery_state: DeliveryState,
        status_code: int | None = None,
        safe_headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
        provider_code: str | None = None,
    ) -> None:
        super().__init__("provider request failed: " + category)
        self.category = category
        self.delivery_state = delivery_state
        self.status_code = status_code
        self.safe_headers = safe_headers or {}
        self.raw_body = raw_body
        self.provider_code = provider_code


def _safe_headers(headers: Message | dict[str, str]) -> dict[str, str]:
    return {
        name.lower(): value
        for name, value in headers.items()
        if name.lower() in _SAFE_HEADERS
        and len(value) <= 128
        and all(32 <= ord(c) < 127 for c in value)
    }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: Message, newurl: str
    ) -> None:
        return None


def _urllib_transport(
    request: Request, *, timeout_seconds: float, response_limit_bytes: int
) -> HttpResponse:
    # No redirects, ambient HTTP proxies or implicit retry handlers.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=timeout_seconds) as response:
        raw = response.read(response_limit_bytes + 1)
        return HttpResponse(response.status, raw, _safe_headers(response.headers))


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON value")


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON key")
        result[name] = value
    return result


def _decode(raw: bytes) -> dict[str, JsonValue]:
    parsed = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    if not isinstance(parsed, dict):
        raise ValueError("response object required")
    pending: list[tuple[JsonValue, int]] = [(parsed, 0)]
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if depth > 64 or nodes > 100000:
            raise ValueError("JSON structural bound exceeded")
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    return cast(dict[str, JsonValue], parsed)


def _optional_string(value: JsonValue) -> str | None:
    if value is not None and (not isinstance(value, str) or len(value) > 1024):
        raise ValueError("invalid response identity")
    return value


def _usage(value: JsonValue) -> TokenUsage | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid usage")
    numbers = [value.get(name) for name in ("prompt_tokens", "completion_tokens", "total_tokens")]
    if any(type(number) is not int or number < 0 for number in numbers):
        raise ValueError("invalid usage counts")
    prompt, completion, total = cast(list[int], numbers)
    if total != prompt + completion:
        raise ValueError("contradictory usage counts")
    return TokenUsage(prompt, completion, total, value)


def prepare_request(
    config: ModelConfig,
    *,
    messages: list[dict[str, JsonValue]],
    tools: list[dict[str, JsonValue]],
    seed: int | None = None,
) -> PreparedRequest:
    """Serialize exactly once; returned bytes are the ledger's network request body."""
    if not messages or any(
        message.get("role") not in ("system", "user", "assistant", "tool") for message in messages
    ):
        raise ProviderError("invalid_request", delivery_state="not_sent")
    if len(tools) > 16 or any(
        tool.get("type") != "function" or not isinstance(tool.get("function"), dict)
        for tool in tools
    ):
        raise ProviderError("invalid_tools", delivery_state="not_sent")
    if seed is not None and (type(seed) is not int or not 0 <= seed <= 2147483647):
        raise ProviderError("invalid_seed", delivery_state="not_sent")
    body: dict[str, JsonValue] = {
        "model": config.model_id,
        "messages": list(messages),
        "tools": list(tools),
        "tool_choice": config.tool_choice if tools else "none",
        "parallel_tool_calls": False,
        "stream": False,
        "n": 1,
        "max_completion_tokens": config.max_completion_tokens,
        "temperature": config.temperature,
        "reasoning_effort": config.reasoning_effort,
        "include_reasoning": True,
        "service_tier": "on_demand",
    }
    if seed is not None:
        body["seed"] = seed
    try:
        encoded = json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ProviderError("invalid_request", delivery_state="not_sent") from None
    if len(encoded) > config.max_request_bytes:
        raise ProviderError("request_too_large", delivery_state="not_sent")
    return PreparedRequest(encoded, hashlib.sha256(encoded).hexdigest(), config_digest(config))


def _parse_response(
    response: HttpResponse, config: ModelConfig, elapsed: float
) -> ProviderResponse:
    headers = _safe_headers(response.safe_headers)
    if len(response.raw_body) > _RESPONSE_LIMIT:
        raise ProviderError(
            "response_too_large",
            delivery_state="response_received",
            status_code=response.status_code,
            safe_headers=headers,
            raw_body=response.raw_body[:_RESPONSE_LIMIT],
        )
    if response.status_code != 200:
        category = "rate_limited" if response.status_code == 429 else "http_error"
        raise ProviderError(
            category,
            delivery_state="response_received",
            status_code=response.status_code,
            safe_headers=headers,
            raw_body=response.raw_body,
        )
    try:
        payload = _decode(response.raw_body)
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError("one choice required")
        choice = choices[0]
        if type(choice.get("index")) is not int or choice.get("index") != 0:
            raise ValueError("single choice index must be zero")
        message, reason = choice.get("message"), choice.get("finish_reason")
        if (
            not isinstance(message, dict)
            or message.get("role") != "assistant"
            or not isinstance(reason, str)
        ):
            raise ValueError("invalid assistant choice")
        if message.get("content") is not None and not isinstance(message.get("content"), str):
            raise ValueError("invalid assistant content")
        if message.get("reasoning") is not None and not isinstance(message.get("reasoning"), str):
            raise ValueError("invalid assistant reasoning")
        tool_calls = message.get("tool_calls")
        if tool_calls is not None and (not isinstance(tool_calls, list) or len(tool_calls) > 16):
            raise ValueError("invalid tool calls")
        returned_model = payload.get("model")
        if returned_model != config.model_id:
            raise ValueError("unexpected model identity")
        return ProviderResponse(
            response.status_code,
            response.raw_body,
            payload,
            message,
            _usage(payload.get("usage")),
            reason,
            config.model_id,
            _optional_string(payload.get("id")),
            _optional_string(payload.get("system_fingerprint")),
            headers,
            elapsed,
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProviderError(
            "invalid_response",
            delivery_state="response_received",
            status_code=response.status_code,
            safe_headers=headers,
            raw_body=response.raw_body,
        ) from None


def config_digest(config: ModelConfig) -> str:
    """Bind honest transport identity and model facts without credentials."""
    raw = json.dumps(
        {"model_config": config.model_dump(mode="json"), "http_user_agent": CLIENT_USER_AGENT},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _validate_prepared(prepared: PreparedRequest, config: ModelConfig) -> None:
    try:
        if (
            len(prepared.body) > config.max_request_bytes
            or hashlib.sha256(prepared.body).hexdigest() != prepared.request_sha256
            or prepared.config_sha256 != config_digest(config)
        ):
            raise ValueError("request identity mismatch")
        body = _decode(prepared.body)
        messages, tools, seed = body.get("messages"), body.get("tools"), body.get("seed")
        if (
            not isinstance(messages, list)
            or any(not isinstance(x, dict) for x in messages)
            or not isinstance(tools, list)
            or any(not isinstance(x, dict) for x in tools)
            or (seed is not None and type(seed) is not int)
        ):
            raise ValueError("invalid prepared body")
        expected = prepare_request(
            config,
            messages=cast(list[dict[str, JsonValue]], messages),
            tools=cast(list[dict[str, JsonValue]], tools),
            seed=seed,
        )
        if expected.body != prepared.body:
            raise ValueError("prepared body differs from selected configuration")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProviderError("invalid_request_identity", delivery_state="not_sent") from None


class ProviderClient:
    def __init__(self, config: ModelConfig, *, transport: HttpTransport | None = None) -> None:
        self.config = config
        self._transport = transport or _urllib_transport

    def complete(self, prepared: PreparedRequest) -> ProviderResponse:
        _validate_prepared(prepared, self.config)
        key = os.environ.get(self.config.provider.api_key_env, "")
        if not key.strip() or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ProviderError("missing_or_invalid_credential", delivery_state="not_sent")
        request = Request(
            self.config.provider.base_url + "/chat/completions",
            data=prepared.body,
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": CLIENT_USER_AGENT,
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            response = self._transport(
                request,
                timeout_seconds=self.config.timeout_seconds,
                response_limit_bytes=_RESPONSE_LIMIT,
            )
        except HTTPError as error:
            try:
                response = HttpResponse(
                    error.code, error.read(_RESPONSE_LIMIT + 1), _safe_headers(error.headers)
                )
            except (OSError, http.client.HTTPException):
                raise ProviderError(
                    "response_read_failed",
                    delivery_state="response_received",
                    status_code=error.code,
                    safe_headers=_safe_headers(error.headers),
                ) from None
            finally:
                error.close()
        except (URLError, OSError, http.client.HTTPException):
            raise ProviderError("transport_failed", delivery_state="possibly_sent") from None
        return _parse_response(response, self.config, time.monotonic() - started)
