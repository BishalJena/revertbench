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
