"""Model clients: a real DeepSeek adapter plus a deterministic offline fake."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .base import (
    BudgetExceededError,
    ChatMessage,
    LLMClient,
    LLMError,
    LLMResponse,
    LLMUnavailableError,
    MissingCredentialError,
    UnknownModelPriceError,
    Usage,
    approx_tokens,
)
from .costing import (
    DEFAULT_CURRENCY,
    DEFAULT_PRICING,
    MeteredLLMClient,
    ModelPrice,
    UsageLedger,
    billing_period,
    build_pricing,
    estimate_cost,
    pricing_from_json,
    resolve_price,
)
from .fake import ScriptExhaustedError, ScriptedLLMClient

__all__ = [
    "BudgetExceededError",
    "ChatMessage",
    "DEFAULT_CURRENCY",
    "DEFAULT_PRICING",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "LLMUnavailableError",
    "MeteredLLMClient",
    "MissingCredentialError",
    "ModelPrice",
    "ScriptExhaustedError",
    "ScriptedLLMClient",
    "UnknownModelPriceError",
    "Usage",
    "UsageLedger",
    "approx_tokens",
    "billing_period",
    "build_client",
    "build_pricing",
    "estimate_cost",
    "pricing_from_json",
    "resolve_price",
]


def build_client(
    settings: Any | None = None,
    *,
    offline: bool = False,
    scripts: Sequence[str] = (),
    by_key: Mapping[str, str] | None = None,
    ledger: UsageLedger | None = None,
) -> LLMClient:
    """Return a metered model client.

    ``offline=True`` builds the scripted fake, which is what the test suite uses;
    otherwise a real DeepSeek client is built from settings (and the API key is
    required at this point, failing fast with a clear message).
    """
    from ..config import load_settings

    resolved = settings if settings is not None else load_settings()
    base: LLMClient
    if offline:
        base = ScriptedLLMClient(scripts, by_key=by_key)
    else:
        from .deepseek import DeepSeekClient

        base = DeepSeekClient(
            api_key=resolved.require_api_key(),
            model=resolved.model,
            base_url=resolved.base_url,
            temperature=resolved.temperature,
            max_tokens=resolved.max_tokens,
            timeout_s=resolved.request_timeout_s,
            max_retries=resolved.max_retries,
        )
    overrides = resolved.model_pricing()
    if ledger is None and (resolved.budget is not None or overrides):
        ledger = UsageLedger(
            limit=resolved.budget,
            pricing=build_pricing(overrides),
            currency=resolved.currency,
        )
    return MeteredLLMClient(base, ledger) if ledger is not None else base
