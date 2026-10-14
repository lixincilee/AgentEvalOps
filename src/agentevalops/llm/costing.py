"""Token pricing, spend accounting, and a metered client wrapper."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from collections.abc import Sequence

from .base import (
    BudgetExceededError,
    ChatMessage,
    LLMClient,
    LLMResponse,
    UnknownModelPriceError,
    Usage,
)

TOKENS_PER_MILLION = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    """Price in USD per 1M tokens."""

    input_per_1m_usd: float
    output_per_1m_usd: float
    cache_hit_input_per_1m_usd: float | None = None

    def cost(self, usage: Usage) -> float:
        # Providers bill cache-hit input tokens at a discount; fall back to the normal
        # input price when the deployment does not price cache hits separately.
        cache_hits = min(usage.cache_hit_tokens, usage.prompt_tokens)
        cache_misses = usage.prompt_tokens - cache_hits
        hit_price = (
            self.input_per_1m_usd
            if self.cache_hit_input_per_1m_usd is None
            else self.cache_hit_input_per_1m_usd
        )
        billed = (
            cache_misses * self.input_per_1m_usd
            + cache_hits * hit_price
            + usage.completion_tokens * self.output_per_1m_usd
        )
        return billed / TOKENS_PER_MILLION

    def to_dict(self) -> dict[str, float]:
        return {
            "input_per_1m_usd": self.input_per_1m_usd,
            "output_per_1m_usd": self.output_per_1m_usd,
            "cache_hit_input_per_1m_usd": self.cache_hit_input_per_1m_usd,
        }


# 模板价目表：仅用于让成本计算可运行。真实端点可能提供完全不同的模型 id
# （例如 deepseek-flash / deepseek-v4-pro），请按你所用端点的实际价目核对后修正，
# 或用环境变量 AGENTEVALOPS_PRICING_JSON 在 .env 中声明，避免改动代码。
DEFAULT_PRICING: dict[str, ModelPrice] = {
    "deepseek-chat": ModelPrice(input_per_1m_usd=0.27, output_per_1m_usd=1.10),
    "deepseek-reasoner": ModelPrice(input_per_1m_usd=0.55, output_per_1m_usd=2.19),
}


def pricing_from_json(
    payload: str, *, source: str = "AGENTEVALOPS_PRICING_JSON"
) -> dict[str, ModelPrice]:
    """Parse a price table from JSON, e.g.

    {"deepseek-flash": {"input_per_1m_usd": 0.1, "output_per_1m_usd": 0.4}}
    """
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source} 不是合法 JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{source} 必须是一个 JSON 对象")

    pricing: dict[str, ModelPrice] = {}
    for model, spec in parsed.items():
        if not isinstance(spec, dict):
            raise ValueError(f"{source} 中 {model!r} 的价目必须是对象")
        try:
            cache_hit = spec.get("cache_hit_input_per_1m_usd")
            pricing[str(model)] = ModelPrice(
                input_per_1m_usd=float(spec["input_per_1m_usd"]),
                output_per_1m_usd=float(spec["output_per_1m_usd"]),
                cache_hit_input_per_1m_usd=None if cache_hit is None else float(cache_hit),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"{source} 中 {model!r} 的价目无效，需要 "
                f"input_per_1m_usd / output_per_1m_usd: {exc}"
            ) from exc
    return pricing


def build_pricing(overrides: Mapping[str, ModelPrice] | None = None) -> dict[str, ModelPrice]:
    pricing = dict(DEFAULT_PRICING)
    if overrides:
        pricing.update(overrides)
    return pricing


def resolve_price(model: str, pricing: Mapping[str, ModelPrice]) -> ModelPrice | None:
    """Exact match first, then the longest known prefix.

    Providers report versioned ids (``deepseek-chat-v3-0324``), which must still be
    priced; the longest matching prefix wins so aliases stay predictable.
    """
    if model in pricing:
        return pricing[model]
    prefixes = [key for key in pricing if model.startswith(key)]
    return pricing[max(prefixes, key=len)] if prefixes else None


def estimate_cost(
    model: str, usage: Usage, pricing: Mapping[str, ModelPrice] | None = None
) -> float | None:
    """Cost of one call, or ``None`` when the model has no configured price."""
    table = DEFAULT_PRICING if pricing is None else pricing
    price = resolve_price(model, table)
    return None if price is None else price.cost(usage)


@dataclass
class UsageLedger:
    """Accumulates tokens and spend across calls, optionally enforcing a ceiling.

    The ceiling is checked *after* each call, since a call's cost is unknowable
    beforehand; the run therefore stops at the first call that crosses the limit.
    """

    limit_usd: float | None = None
    pricing: dict[str, ModelPrice] = field(default_factory=build_pricing)
    strict_pricing: bool = False
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    unpriced_models: set[str] = field(default_factory=set)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def all_calls_priced(self) -> bool:
        return not self.unpriced_models

    @property
    def cost_is_complete(self) -> bool:
        """False when some spend could not be priced, so totals are a lower bound."""
        return not self.unpriced_models

    def record(self, model: str, usage: Usage) -> float:
        """Record one call and return its cost.

        Raise :class:`UnknownModelPriceError` for an unpriced model unless
        ``strict_pricing`` was disabled, so unmetered spend can never be invisible.
        """
        price = resolve_price(model, self.pricing)
        if price is None:
            self.unpriced_models.add(model)
            call_cost = 0.0
        else:
            call_cost = price.cost(usage)
        self.calls += 1
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.cost_usd += call_cost
        if price is None and (self.strict_pricing or self.limit_usd is not None):
            raise UnknownModelPriceError(
                f"模型 {model!r} 没有价目，无法计入成本"
                f"{'，预算上限形同虚设' if self.limit_usd is not None else ''}。"
                f"请在 pricing 中补充该模型价格"
                f"（可用 AGENTEVALOPS_PRICING_JSON 在 .env 里声明），"
                f"或设置 strict_pricing=False 并接受成本只算下限。"
            )
        return call_cost

    def assert_within_budget(self) -> None:
        if self.limit_usd is not None and self.cost_usd >= self.limit_usd:
            raise BudgetExceededError(
                f"本次运行已花费 ${self.cost_usd:.6f}，达到或超过预算上限 ${self.limit_usd:.6f}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "limit_usd": self.limit_usd,
            "strict_pricing": self.strict_pricing,
            "cost_is_complete": self.cost_is_complete,
            "unpriced_models": sorted(self.unpriced_models),
        }


class MeteredLLMClient:
    """Wraps any client so every call is recorded and budget-checked."""

    def __init__(self, client: LLMClient, ledger: UsageLedger | None = None) -> None:
        self.client = client
        self.ledger = ledger if ledger is not None else UsageLedger()

    @property
    def model_id(self) -> str:
        return self.client.model_id

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        response = self.client.complete(
            messages, temperature=temperature, max_tokens=max_tokens
        )
        self.ledger.record(response.model, response.usage)
        self.ledger.assert_within_budget()
        return response
