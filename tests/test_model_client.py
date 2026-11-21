import hashlib
import http.client
import io
import json
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from revertbench.models.client import (
    HttpResponse,
    PreparedRequest,
    ProviderClient,
    ProviderError,
    TokenUsage,
    config_digest,
    prepare_request,
)
from revertbench.models.config import ModelConfig, load_model


def model() -> ModelConfig:
    return load_model(Path("configs/models.example.toml"), "groq-120b")


class FakeTransport:
    def __init__(self, raw: bytes, status: int = 200) -> None:
        self.raw = raw
        self.status = status
        self.calls: list[Request] = []

    def __call__(
        self, request: Request, *, timeout_seconds: float, response_limit_bytes: int
    ) -> HttpResponse:
        self.calls.append(request)
        return HttpResponse(
            self.status,
            self.raw,
            {"x-ratelimit-limit-requests": "1000", "set-cookie": "private-cookie"},
        )


def payload() -> dict[str, object]:
    return {
        "id": "completion-1",
        "model": "openai/gpt-oss-120b",
        "system_fingerprint": "backend-1",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning": "Retained provider reasoning.",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "list_dir", "arguments": '{"path":"."}'},
                        }
                    ],
                },
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 40,
            "total_tokens": 140,
            "completion_tokens_details": {"reasoning_tokens": 20},
        },
    }


def prepared() -> PreparedRequest:
    return prepare_request(
        model(),
        messages=[{"role": "user", "content": "Find the regression."}],
        tools=[
            {"type": "function", "function": {"name": "list_dir", "parameters": {"type": "object"}}}
        ],
    )


def test_request_and_response_preserve_full_tool_turn_without_host_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "secret-sentinel")
    transport = FakeTransport(json.dumps(payload()).encode())
    client = ProviderClient(model(), transport=transport)
    request = prepared()
    result = client.complete(request)
    assert result.assistant_message == json.loads(transport.raw)["choices"][0]["message"]
    assert result.usage == TokenUsage(100, 40, 140, json.loads(transport.raw)["usage"])
    assert result.raw_body == transport.raw
    assert result.system_fingerprint == "backend-1"
    assert result.safe_headers == {"x-ratelimit-limit-requests": "1000"}
    followup = prepare_request(
        model(),
        messages=[
            {"role": "user", "content": "Find the regression."},
            result.assistant_message,
            {"role": "tool", "tool_call_id": "call-1", "content": "{}"},
        ],
        tools=[],
    )
    body = json.loads(followup.body)
    assert body["messages"][1]["reasoning"] == "Retained provider reasoning."
    assert body["messages"][1]["tool_calls"][0]["function"]["arguments"] == '{"path":"."}'
    assert body["stream"] is False and body["parallel_tool_calls"] is False
    assert body["max_completion_tokens"] == 2048 and "reasoning_format" not in body
    assert body["include_reasoning"] is True
    assert request.request_sha256 == hashlib.sha256(request.body).hexdigest()
    assert b"secret-sentinel" not in request.body
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status", [301, 400, 401, 429, 500])
def test_http_failure_is_one_attempt_with_safe_diagnostics(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "secret-sentinel")
    transport = FakeTransport(
        b'{"error":{"message":"secret-sentinel private-prompt","code":"rate_limit"}}', status
    )
    with pytest.raises(ProviderError) as caught:
        ProviderClient(model(), transport=transport).complete(prepared())
    assert len(transport.calls) == 1
    assert caught.value.status_code == status
    assert caught.value.delivery_state == "response_received"
    assert "secret-sentinel" not in str(caught.value) and "private-prompt" not in repr(caught.value)
    assert caught.value.raw_body == transport.raw


@pytest.mark.parametrize("error", [TimeoutError("secret-sentinel"), URLError("private-prompt")])
def test_uncertain_transport_failure_has_no_implicit_retry(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "secret-sentinel")
    calls: list[Request] = []

    def fail(
        request: Request, *, timeout_seconds: float, response_limit_bytes: int
    ) -> HttpResponse:
        calls.append(request)
        raise error

    with pytest.raises(ProviderError) as caught:
        ProviderClient(model(), transport=fail).complete(prepared())
    assert len(calls) == 1 and caught.value.delivery_state == "possibly_sent"
    assert "secret-sentinel" not in str(caught.value)


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b'{"choices":[],"choices":[]}',
        b'{"x":NaN}',
        b"[" * 1000 + b"]" * 1000,
        b"x" * (2097152 + 1),
    ],
)
def test_invalid_bounded_response_is_uncertain_with_raw_evidence(
    monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "secret-sentinel")
    with pytest.raises(ProviderError) as caught:
        ProviderClient(model(), transport=FakeTransport(raw)).complete(prepared())
    assert caught.value.delivery_state == "response_received"
    assert caught.value.category in {"invalid_response", "response_too_large"}


def test_missing_usage_stays_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "secret-sentinel")
    data = payload()
    del data["usage"]
    result = ProviderClient(model(), transport=FakeTransport(json.dumps(data).encode())).complete(
        prepared()
    )
    assert result.usage is None


def test_missing_key_fails_before_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    transport = FakeTransport(b"")
    with pytest.raises(ProviderError) as caught:
        ProviderClient(model(), transport=transport).complete(prepared())
    assert caught.value.delivery_state == "not_sent" and not transport.calls


def test_urllib_http_error_body_is_not_misclassified_as_unknown_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "secret-sentinel")

    def fail(
        request: Request, *, timeout_seconds: float, response_limit_bytes: int
    ) -> HttpResponse:
        headers = Message()
        headers["retry-after"] = "2"
        raise HTTPError(
            request.full_url,
            429,
            "secret-sentinel",
            headers,
            io.BytesIO(b'{"error":{"type":"rate_limit"}}'),
        )

    with pytest.raises(ProviderError) as caught:
        ProviderClient(model(), transport=fail).complete(prepared())
    assert caught.value.status_code == 429 and caught.value.delivery_state == "response_received"
    assert caught.value.safe_headers["retry-after"] == "2"
    assert caught.value.raw_body == b'{"error":{"type":"rate_limit"}}'


def test_transport_does_not_inherit_proxy_or_follow_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.request import HTTPRedirectHandler, ProxyHandler

    import revertbench.models.client as module

    monkeypatch.setenv("GROQ_API_KEY", "secret-sentinel")
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted-proxy.invalid")
    handlers: list[object] = []

    class Opener:
        def open(self, request: Request, *, timeout: float) -> io.BytesIO:
            assert request.full_url == "https://api.groq.com/openai/v1/chat/completions"
            raise HTTPError(request.full_url, 302, "Found", Message(), io.BytesIO())

    def build(*items: object) -> Opener:
        handlers.extend(items)
        return Opener()

    monkeypatch.setattr(module, "build_opener", build)
    with pytest.raises(ProviderError) as caught:
        ProviderClient(model()).complete(prepared())
    assert caught.value.status_code == 302
    proxy = next(x for x in handlers if isinstance(x, ProxyHandler))
    assert vars(proxy)["proxies"] == {}
    redirect = next(x for x in handlers if isinstance(x, HTTPRedirectHandler))
    assert (
        redirect.redirect_request(
            Request("https://api.groq.com"),
            io.BytesIO(),
            302,
            "",
            http.client.HTTPMessage(),
            "https://other.invalid",
        )
        is None
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "other-provider/model"),
        ("max_completion_tokens", 65536),
        ("stream", True),
        ("tools", [{"type": "browser_search"}]),
        ("service_tier", "auto"),
    ],
)
def test_prepared_bytes_cannot_override_reserved_model_or_caps(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "secret-sentinel")
    body = json.loads(prepared().body)
    body[field] = value
    raw = json.dumps(body).encode()
    transport = FakeTransport(json.dumps(payload()).encode())
    with pytest.raises(ProviderError) as caught:
        ProviderClient(model(), transport=transport).complete(
            PreparedRequest(raw, hashlib.sha256(raw).hexdigest(), config_digest(model()))
        )
    assert caught.value.delivery_state == "not_sent" and not transport.calls


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": True, "completion_tokens": 0, "total_tokens": 1},
        {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 1},
        {"prompt_tokens": 1},
    ],
)
def test_malformed_usage_is_not_silently_settled_as_zero(
    monkeypatch: pytest.MonkeyPatch, usage: dict[str, object]
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "secret-sentinel")
    data = payload()
    data["usage"] = usage
    with pytest.raises(ProviderError, match="invalid_response"):
        ProviderClient(model(), transport=FakeTransport(json.dumps(data).encode())).complete(
            prepared()
        )


def test_credential_is_not_read_by_config_or_stored_in_client_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "secret-sentinel")
    client = ProviderClient(model(), transport=FakeTransport(json.dumps(payload()).encode()))
    assert "secret-sentinel" not in repr(client) + repr(client.config)
    client.complete(prepared())
    assert "secret-sentinel" not in repr(client)


def test_request_is_bound_to_price_and_quota_config_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "secret-sentinel")
    changed = model().model_dump(mode="python")
    changed["max_completion_tokens"] = 1024
    transport = FakeTransport(json.dumps(payload()).encode())
    with pytest.raises(ProviderError) as caught:
        ProviderClient(ModelConfig.model_validate(changed), transport=transport).complete(
            prepared()
        )
    assert caught.value.delivery_state == "not_sent" and not transport.calls


def test_nonzero_choice_index_is_invalid_for_one_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "secret-sentinel")
    data = json.loads(json.dumps(payload()))
    data["choices"][0]["index"] = 3
    with pytest.raises(ProviderError, match="invalid_response"):
        ProviderClient(model(), transport=FakeTransport(json.dumps(data).encode())).complete(
            prepared()
        )


def test_unhashable_message_role_is_a_safe_preflight_failure() -> None:
    with pytest.raises(ProviderError) as caught:
        prepare_request(model(), messages=[{"role": []}], tools=[])
    assert caught.value.delivery_state == "not_sent"


def test_default_transport_caps_body_and_closes_response(monkeypatch: pytest.MonkeyPatch) -> None:
    import revertbench.models.client as module

    monkeypatch.setenv("GROQ_API_KEY", "secret-sentinel")

    class Response:
        status = 200
        headers = Message()
        closed = False

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, kind: object, error: object, traceback: object) -> None:
            self.closed = True

        def read(self, size: int) -> bytes:
            assert size == 2097153
            return b"x" * size

    response = Response()

    class Opener:
        def open(self, request: Request, *, timeout: float) -> Response:
            assert timeout == model().timeout_seconds
            return response

    monkeypatch.setattr(module, "build_opener", lambda *args: Opener())
    with pytest.raises(ProviderError) as caught:
        ProviderClient(model()).complete(prepared())
    assert caught.value.category == "response_too_large"
    assert caught.value.raw_body is not None and len(caught.value.raw_body) == 2097152
    assert response.closed


def test_unknown_assistant_fields_are_retained_in_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "secret-sentinel")
    data = json.loads(json.dumps(payload()))
    data["choices"][0]["message"]["provider_extension"] = {"opaque": "preserved"}
    result = ProviderClient(model(), transport=FakeTransport(json.dumps(data).encode())).complete(
        prepared()
    )
    followup = prepare_request(model(), messages=[result.assistant_message], tools=[])
    assert json.loads(followup.body)["messages"][0]["provider_extension"] == {"opaque": "preserved"}


def test_client_sends_its_honest_application_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "secret-sentinel")
    transport = FakeTransport(json.dumps(payload()).encode())
    ProviderClient(model(), transport=transport).complete(prepared())
    assert transport.calls[0].get_header("User-agent") == "RevertBench/0.0.1"


def test_user_agent_change_invalidates_prepared_transport_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import revertbench.models.client as module

    monkeypatch.setenv("GROQ_API_KEY", "secret-sentinel")
    request = prepared()
    original = config_digest(model())
    monkeypatch.setattr(module, "CLIENT_USER_AGENT", "RevertBench/test-version")
    assert config_digest(model()) != original
    transport = FakeTransport(json.dumps(payload()).encode())
    with pytest.raises(ProviderError) as caught:
        ProviderClient(model(), transport=transport).complete(request)
    assert caught.value.delivery_state == "not_sent" and not transport.calls
