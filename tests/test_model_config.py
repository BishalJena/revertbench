from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from revertbench.models.config import ConfigError, ModelConfig, load_model


def config_data() -> dict[str, object]:
    return {
        "alias": "pilot",
        "provider": {
            "id": "groq",
            "base_url": "https://api.groq.com/openai/v1",
            "api_key_env": "GROQ_API_KEY",
        },
        "model_id": "openai/gpt-oss-120b",
        "model_context_tokens": 131072,
        "max_completion_tokens": 2048,
        "pricing": {
            "input_usd_per_million": "0.15",
            "output_usd_per_million": "0.60",
            "source_url": "https://console.groq.com/docs/model/openai/gpt-oss-120b",
            "observed_at": datetime(2026, 11, 21, tzinfo=UTC),
        },
        "quotas": {
            "rpm": 1,
            "tpm": 8000,
            "rpd": 1000,
            "tpd": 200000,
            "evidence": "conservative",
            "source_url": "https://console.groq.com/docs/rate-limits",
            "observed_at": datetime(2026, 11, 21, tzinfo=UTC),
        },
    }


def model() -> ModelConfig:
    return ModelConfig.model_validate(config_data())


def test_load_selected_explicit_model_without_reading_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    path = tmp_path / "models.toml"
    path.write_text(Path("configs/models.example.toml").read_text())
    selected = load_model(path, "groq-120b")
    assert selected.provider.base_url == "https://api.groq.com/openai/v1"
    assert selected.model_id == "openai/gpt-oss-120b"
    assert selected.max_completion_tokens == 2048
    assert selected.tool_choice == "required"
    assert selected.pricing.input_usd_per_million == Decimal("0.15")
    assert selected.quotas.evidence == "conservative"
    assert "Bearer" not in selected.model_dump_json()


@pytest.mark.parametrize(
    "url",
    [
        "http://api.groq.com/v1",
        "https://key@api.groq.com/v1",
        "https://api.groq.com/v1?secret=x",
        "https://api.groq.com/v1#x",
        "https://api.groq.com/\nsecret",
    ],
)
def test_provider_url_cannot_leak_credentials_or_use_cleartext(url: str) -> None:
    data = config_data()
    data["provider"] = {"id": "groq", "base_url": url, "api_key_env": "GROQ_API_KEY"}
    with pytest.raises(ValueError):
        ModelConfig.model_validate(data)


@pytest.mark.parametrize(
    "changes",
    [
        {"model_id": ""},
        {"model_context_tokens": True},
        {"max_completion_tokens": 0},
        {"max_completion_tokens": 200000},
        {"temperature": float("nan")},
        {"timeout_seconds": float("inf")},
        {"model_revision": ""},
    ],
)
def test_reject_invalid_model_bounds(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ModelConfig.model_validate({**config_data(), **changes})


def test_unknown_price_or_unaware_evidence_rejected() -> None:
    data = config_data()
    data["pricing"] = {
        "input_usd_per_million": "-1",
        "output_usd_per_million": "NaN",
        "source_url": "https://example.com",
        "observed_at": datetime(2026, 1, 1),
    }
    with pytest.raises(ValueError):
        ModelConfig.model_validate(data)


def test_config_load_errors_are_sanitized(tmp_path: Path) -> None:
    path = tmp_path / "models.toml"
    path.write_text("private-sentinel-key = [invalid")
    with pytest.raises(ConfigError) as caught:
        load_model(path, "unknown")
    assert "private-sentinel" not in str(caught.value)
    with pytest.raises(ConfigError):
        load_model(tmp_path / "missing.toml", "unknown")


def test_duplicate_alias_is_not_silently_selected(tmp_path: Path) -> None:
    source = Path("configs/models.example.toml").read_text()
    path = tmp_path / "models.toml"
    path.write_text(source + "\n" + source[source.index("[[models]]") :])
    with pytest.raises(ConfigError):
        load_model(path, "groq-120b")


def test_first_provider_profile_does_not_promise_arbitrary_compatibility() -> None:
    data = config_data()
    data["provider"] = {
        "id": "other",
        "base_url": "https://example.com/v1",
        "api_key_env": "OTHER_API_KEY",
    }
    with pytest.raises(ValueError):
        ModelConfig.model_validate(data)


def test_missing_tool_selector_retains_auto_default() -> None:
    assert model().tool_choice == "auto"


@pytest.mark.parametrize("choice", ["none", "mandatory", "", True, {"name": "submit"}])
def test_unsupported_tool_selector_is_not_passthrough(choice: object) -> None:
    with pytest.raises(ValueError):
        ModelConfig.model_validate({**config_data(), "tool_choice": choice})


def qwen_data() -> dict[str, object]:
    data = config_data()
    data.update(
        alias="qwen-candidate",
        model_id="qwen/qwen3.8-27b",
        profile="qwen3_8_parsed",
        reasoning_effort="low",
    )
    data["pricing"] = {
        "input_usd_per_million": "0.80",
        "output_usd_per_million": "4.00",
        "source_url": "https://console.groq.com/docs/model/qwen/qwen3.8-27b",
        "observed_at": datetime(2026, 11, 21, tzinfo=UTC),
    }
    return data


def test_explicit_qwen_profile_retains_unknown_hosted_revision_and_cutoff() -> None:
    selected = ModelConfig.model_validate(qwen_data())
    assert selected.profile == "qwen3_8_parsed" and selected.model_id == "qwen/qwen3.8-27b"
    assert selected.model_revision is None and selected.declared_cutoff is None
    assert selected.cutoff_source_url is None
    assert selected.model_context_tokens == 131072
    assert selected.max_completion_tokens == 2048
    assert selected.pricing.input_usd_per_million == Decimal("0.80")
    assert selected.pricing.output_usd_per_million == Decimal("4.00")


@pytest.mark.parametrize(
    "model_id", ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]
)
@pytest.mark.parametrize("context", [4096, 131071, 131073])
def test_native_context_fact_cannot_shrink_financial_reservations(
    model_id: str, context: int
) -> None:
    data = qwen_data() if model_id.startswith("qwen/") else config_data()
    data.update(model_id=model_id, model_context_tokens=context)
    with pytest.raises(ValueError):
        ModelConfig.model_validate(data)


@pytest.mark.parametrize(
    "changes",
    [
        {"profile": "gpt_oss"},
        {"profile": "auto"},
        {"profile": True},
        {"model_id": "qwen/qwen3.6-27b"},
        {"model_id": "qwen/qwen3.8-27b:latest"},
        {"max_completion_tokens": 16385},
        {"reasoning_effort": "medium"},
        {"reasoning_effort": "default"},
    ],
)
def test_qwen_rejects_unsupported_or_ambiguous_profile_options(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ModelConfig.model_validate({**qwen_data(), **changes})


def test_qwen_requires_explicit_profile_and_gpt_oss_rejects_qwen_profile() -> None:
    missing = qwen_data()
    missing.pop("profile")
    with pytest.raises(ValueError):
        ModelConfig.model_validate(missing)
    with pytest.raises(ValueError):
        ModelConfig.model_validate({**config_data(), "profile": "qwen3_8_parsed"})


@pytest.mark.parametrize("maximum", [512, 16384])
def test_qwen_native_output_boundary_is_usable(maximum: int) -> None:
    selected = ModelConfig.model_validate({**qwen_data(), "max_completion_tokens": maximum})
    assert selected.max_completion_tokens == maximum


def test_gpt_oss_keeps_its_default_profile_and_native_output_cap() -> None:
    selected = ModelConfig.model_validate({**config_data(), "max_completion_tokens": 65536})
    assert selected.profile == "gpt_oss"
    assert selected.max_completion_tokens == 65536
