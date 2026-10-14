"""Environment-driven settings, plus a dependency-free ``.env`` reader.

Real environment variables always win over ``.env`` values, so CI can inject secrets
without touching files.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from .llm.base import MissingCredentialError
from .llm.costing import DEFAULT_CURRENCY, ModelPrice, pricing_from_json

DEFAULT_ENV_FILE = Path(".env")
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


class ConfigurationError(ValueError):
    """Raised when a setting is present but cannot be parsed."""


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse the ``KEY=VALUE`` subset of dotenv syntax (no dependencies)."""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def read_dotenv(env_file: Path | None) -> dict[str, str]:
    if env_file is None or not env_file.is_file():
        return {}
    # utf-8-sig transparently strips a byte-order mark. Windows editors (Notepad among
    # them) add one, and a leading BOM would otherwise rename the first key -- silently
    # dropping the API key and looking exactly like "the key was never configured".
    return parse_dotenv(env_file.read_text(encoding="utf-8-sig"))


def _first(*candidates: str | None) -> str | None:
    for candidate in candidates:
        if candidate is not None and candidate != "":
            return candidate
    return None


def _as_float(name: str, value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是数字，实际是 {value!r}") from exc


def _as_int(name: str, value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是整数，实际是 {value!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Everything the platform needs to talk to the model provider."""

    api_key: str | None = None
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: float = 0.0
    max_tokens: int = 1024
    request_timeout_s: float = 60.0
    max_retries: int = 2
    budget: float | None = None
    currency: str = DEFAULT_CURRENCY
    pricing_json: str | None = None

    @property
    def llm_configured(self) -> bool:
        """True when a real provider call is possible; False means offline only."""
        return bool(self.api_key and self.api_key.strip())

    def require_api_key(self) -> str:
        if not self.llm_configured:
            raise MissingCredentialError(
                "未找到 DeepSeek API Key。请在项目根目录的 .env 里写入 "
                "DEEPSEEK_API_KEY=...（该文件已被 .gitignore 忽略），"
                "或设置同名环境变量。离线评测请改用 ScriptedLLMClient。"
            )
        assert self.api_key is not None
        return self.api_key

    def model_pricing(self) -> dict[str, ModelPrice]:
        """Per-deployment price overrides parsed from ``AGENTEVALOPS_PRICING_JSON``."""
        if not self.pricing_json:
            return {}
        try:
            return pricing_from_json(
                self.pricing_json, default_currency=self.currency
            )
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc


def load_settings(
    *,
    env_file: Path | None = DEFAULT_ENV_FILE,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Build settings from the environment, falling back to ``.env``."""
    env = os.environ if environ is None else environ
    dotenv = read_dotenv(env_file)

    def lookup(name: str) -> str | None:
        return _first(env.get(name), dotenv.get(name))

    defaults = Settings()
    temperature = _as_float(
        "AGENTEVALOPS_TEMPERATURE", lookup("AGENTEVALOPS_TEMPERATURE")
    )
    max_tokens = _as_int("AGENTEVALOPS_MAX_TOKENS", lookup("AGENTEVALOPS_MAX_TOKENS"))
    timeout = _as_float(
        "AGENTEVALOPS_REQUEST_TIMEOUT_S", lookup("AGENTEVALOPS_REQUEST_TIMEOUT_S")
    )
    retries = _as_int("AGENTEVALOPS_MAX_RETRIES", lookup("AGENTEVALOPS_MAX_RETRIES"))
    budget = _as_float("AGENTEVALOPS_BUDGET", lookup("AGENTEVALOPS_BUDGET"))

    return replace(
        defaults,
        api_key=lookup("DEEPSEEK_API_KEY"),
        base_url=lookup("DEEPSEEK_BASE_URL") or defaults.base_url,
        model=lookup("DEEPSEEK_MODEL") or defaults.model,
        temperature=defaults.temperature if temperature is None else temperature,
        max_tokens=defaults.max_tokens if max_tokens is None else max_tokens,
        request_timeout_s=defaults.request_timeout_s if timeout is None else timeout,
        max_retries=defaults.max_retries if retries is None else retries,
        budget=budget,
        currency=lookup("AGENTEVALOPS_CURRENCY") or defaults.currency,
        pricing_json=lookup("AGENTEVALOPS_PRICING_JSON"),
    )
