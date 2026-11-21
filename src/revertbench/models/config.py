"""Explicit, secret-free model configuration and dated provider facts."""

import re
import tomllib
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

PositiveInt = Annotated[int, Field(strict=True, gt=0)]
NonnegativePrice = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class ConfigError(ValueError):
    """Sanitized configuration diagnostic without input values or secrets."""


def _url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(ord(c) < 33 for c in value)
    ):
        raise ValueError("URL must use HTTPS without credentials, query or fragment")
    return value.rstrip("/")


class SafeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class ProviderConfig(SafeConfig):
    id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")]
    base_url: str
    api_key_env: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")]

    _base_url = field_validator("base_url")(_url)


class TokenPricing(SafeConfig):
    input_usd_per_million: NonnegativePrice
    output_usd_per_million: NonnegativePrice
    source_url: str
    observed_at: AwareDatetime

    _source_url = field_validator("source_url")(_url)


class RateLimits(SafeConfig):
    rpm: PositiveInt
    tpm: PositiveInt
    rpd: PositiveInt
    tpd: PositiveInt
    evidence: Literal["conservative", "observed"]
    source_url: str
    observed_at: AwareDatetime

    _source_url = field_validator("source_url")(_url)


class ModelConfig(SafeConfig):
    alias: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")]
    provider: ProviderConfig
    model_id: Annotated[str, Field(min_length=1, max_length=256)]
    profile: Literal["gpt_oss", "qwen3_8_parsed"] = "gpt_oss"
    model_context_tokens: PositiveInt
    max_completion_tokens: PositiveInt = 2048
    tool_choice: Literal["auto", "required"] = "auto"
    temperature: Annotated[float, Field(ge=0, le=2, allow_inf_nan=False)] = 0.6
    reasoning_effort: Literal["low", "medium", "high"] = "low"
    timeout_seconds: Annotated[float, Field(gt=0, le=120, allow_inf_nan=False)] = 60
    max_request_bytes: Annotated[int, Field(strict=True, gt=0, le=2097152)] = 262144
    pricing: TokenPricing
    quotas: RateLimits
    model_revision: str | None = None
    declared_cutoff: str | None = None
    cutoff_source_url: str | None = None

    @field_validator("model_id", "model_revision")
    @classmethod
    def nonblank_identity(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(ord(c) < 32 for c in value)):
            raise ValueError("model identity must be nonblank without controls")
        return value

    @field_validator("declared_cutoff")
    @classmethod
    def cutoff_precision(cls, value: str | None) -> str | None:
        if value is not None:
            if re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", value) is None:
                raise ValueError("cutoff must retain its declared date precision")
            datetime.fromisoformat(value + "-01" if len(value) == 7 else value)
        return value

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if self.provider.id != "groq" or self.provider.base_url != "https://api.groq.com/openai/v1":
            raise ValueError("only the documented Groq profiles are supported")
        if self.profile == "gpt_oss":
            if self.model_id not in {"openai/gpt-oss-120b", "openai/gpt-oss-20b"}:
                raise ValueError("GPT-OSS profile requires an exact supported model ID")
            native_output_limit = 65536
        else:
            if self.model_id != "qwen/qwen3.8-27b" or self.reasoning_effort != "low":
                raise ValueError("Qwen parsed profile requires its exact model ID and low effort")
            native_output_limit = 16384
        if self.model_context_tokens != 131072:
            raise ValueError("native model context must remain 131072 for financial reservations")
        if self.max_completion_tokens > native_output_limit:
            raise ValueError("completion exceeds the profile's native output limit")
        if (self.declared_cutoff is None) != (self.cutoff_source_url is None):
            raise ValueError("declared cutoff requires its source")
        if self.cutoff_source_url is not None:
            _url(self.cutoff_source_url)
        return self

    @property
    def assistant_projection(self) -> str:
        """A fixed wire-history policy, separately bound into request identity."""
        return (
            "qwen-assistant-without-reasoning-v1"
            if self.profile == "qwen3_8_parsed"
            else "assistant-verbatim-v1"
        )


def load_model(path: Path, alias: str) -> ModelConfig:
    """Read bounded TOML; resolve the explicit alias without accessing credentials."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(1048577)
        if len(raw) > 1048576:
            raise ConfigError("model configuration exceeds size limit")
        data = tomllib.loads(raw.decode("utf-8"))
        if set(data) != {"providers", "models"}:
            raise ConfigError("model configuration requires providers and models")
        providers = data["providers"]
        models = data["models"]
        if not isinstance(providers, dict) or not isinstance(models, list):
            raise ConfigError("model configuration has an invalid structure")
        selected: ModelConfig | None = None
        seen: set[str] = set()
        for row in models:
            if not isinstance(row, dict):
                raise ConfigError("model configuration has an invalid entry")
            name, provider_id = row.get("alias"), row.get("provider")
            if not isinstance(name, str) or name in seen or not isinstance(provider_id, str):
                raise ConfigError("model aliases must be explicit and unique")
            seen.add(name)
            provider = providers.get(provider_id)
            if not isinstance(provider, dict):
                raise ConfigError("model references an undefined provider")
            candidate = ModelConfig.model_validate(
                {**row, "provider": {"id": provider_id, **provider}}
            )
            if name == alias:
                selected = candidate
        if selected is None:
            raise ConfigError("requested model alias is not configured")
        return selected
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValidationError, ValueError) as error:
        if isinstance(error, ConfigError):
            raise
        raise ConfigError("model configuration is unreadable or invalid") from None
