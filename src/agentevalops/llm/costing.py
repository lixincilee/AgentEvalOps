"""Token pricing, spend accounting, and a metered client wrapper."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .base import (
    BudgetExceededError,
    ChatMessage,
    LLMClient,
    LLMResponse,
    UnknownModelPriceError,
    Usage,
)

TOKENS_PER_MILLION = 1_000_000

# 价目单位是**人民币元**（供应商账单就是元）。把「元」当成「美元」是账目上最容易犯、
# 也最贵的错误，所以字段名一律不带 _usd，货币由 ModelPrice.currency 显式声明。
DEFAULT_CURRENCY = "CNY"

# 中国自 1991 年起全境单一时区、无夏令时，固定 UTC+8 就是准确的，不必引入 tzdata。
CHINA_TZ = timezone(timedelta(hours=8))
PEAK_HOUR_RANGES = ((9, 12), (14, 18))
OFF_PEAK_MULTIPLIER = 0.5
PEAK = "peak"
OFF_PEAK = "off_peak"


def billing_period(now: datetime | None = None) -> str:
    """Return ``peak`` or ``off_peak`` for a moment in Beijing time.

    高峰 = 周一至周五 9:00-12:00 / 14:00-18:00。法定节假日日历没有建模，所以节假日会被
    按高峰价计费 -- 误差方向是「多算」而不是「少算」，成本报告不会因此虚低。
    """
    moment = datetime.now(CHINA_TZ) if now is None else now
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CHINA_TZ)
    moment = moment.astimezone(CHINA_TZ)
    if moment.weekday() >= 5:
        return OFF_PEAK
    return PEAK if any(start <= moment.hour < end for start, end in PEAK_HOUR_RANGES) else OFF_PEAK


@dataclass(frozen=True)
class ModelPrice:
    """Price per 1M tokens in ``currency``, quoted at the **peak** rate.

    供应商把空闲时段定义为高峰时段的一半，所以一对高峰价 + 一个倍率就能表示两档，
    不必把六个数字抄两遍。
    """

    input_per_1m: float
    output_per_1m: float
    cache_hit_input_per_1m: float | None = None
    currency: str = DEFAULT_CURRENCY
    off_peak_multiplier: float = OFF_PEAK_MULTIPLIER

    def rates(self, *, off_peak: bool = False) -> tuple[float, float, float]:
        """``(input, output, cache_hit_input)`` per 1M tokens for the given period."""
        factor = self.off_peak_multiplier if off_peak else 1.0
        hit = self.input_per_1m if self.cache_hit_input_per_1m is None else self.cache_hit_input_per_1m
        return (self.input_per_1m * factor, self.output_per_1m * factor, hit * factor)

    def cost(self, usage: Usage, *, off_peak: bool = False) -> float:
        # Providers bill cache-hit input tokens at a discount; fall back to the normal
        # input price when the deployment does not price cache hits separately.
        input_rate, output_rate, hit_rate = self.rates(off_peak=off_peak)
        cache_hits = min(usage.cache_hit_tokens, usage.prompt_tokens)
        cache_misses = usage.prompt_tokens - cache_hits
        billed = (
            cache_misses * input_rate
            + cache_hits * hit_rate
            + usage.completion_tokens * output_rate
        )
        return billed / TOKENS_PER_MILLION

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_per_1m": self.input_per_1m,
            "output_per_1m": self.output_per_1m,
            "cache_hit_input_per_1m": self.cache_hit_input_per_1m,
            "off_peak_multiplier": self.off_peak_multiplier,
            "currency": self.currency,
        }


# 真实价目表（人民币元 / 百万 tokens，**高峰价**），来源：供应商官方价目页。
#   deepseek-flash  : 缓存未命中输入 2 元、缓存命中输入 0.04 元、输出 8 元
#   deepseek-v4-pro : 缓存未命中输入 9 元、缓存命中输入 0.30 元、输出 27 元
# 空闲时段统一为高峰价的一半（由 off_peak_multiplier 处理）。
# 本表刻意**不**收录 deepseek-chat / deepseek-reasoner：本端点会把它们按别名解析成
# deepseek-flash 计费，而官方美元价目与元价目混在一张表里必然算错。
# 需要别的模型请在 .env 用 AGENTEVALOPS_PRICING_JSON 声明，不要改这里的数字。
DEFAULT_PRICING: dict[str, ModelPrice] = {
    "deepseek-flash": ModelPrice(
        input_per_1m=2.0, output_per_1m=8.0, cache_hit_input_per_1m=0.04
    ),
    "deepseek-v4-pro": ModelPrice(
        input_per_1m=9.0, output_per_1m=27.0, cache_hit_input_per_1m=0.30
    ),
}


def pricing_from_json(
    payload: str,
    *,
    source: str = "AGENTEVALOPS_PRICING_JSON",
    default_currency: str = DEFAULT_CURRENCY,
) -> dict[str, ModelPrice]:
    """Parse a price table from JSON, e.g.

    {"deepseek-flash": {"input_per_1m": 2.0, "output_per_1m": 8.0,
                        "cache_hit_input_per_1m": 0.04, "currency": "CNY"}}

    ``input_per_1m`` / ``output_per_1m`` are the **peak** rates. ``currency`` defaults to
    ``default_currency`` so a table never silently mixes 元 and 美元.
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
            cache_hit = spec.get("cache_hit_input_per_1m")
            off_peak = spec.get("off_peak_multiplier")
            pricing[str(model)] = ModelPrice(
                input_per_1m=float(spec["input_per_1m"]),
                output_per_1m=float(spec["output_per_1m"]),
                cache_hit_input_per_1m=None if cache_hit is None else float(cache_hit),
                currency=str(spec.get("currency") or default_currency),
                off_peak_multiplier=(
                    OFF_PEAK_MULTIPLIER if off_peak is None else float(off_peak)
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"{source} 中 {model!r} 的价目无效，需要 "
                f"input_per_1m / output_per_1m: {exc}"
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
    model: str,
    usage: Usage,
    pricing: Mapping[str, ModelPrice] | None = None,
    *,
    off_peak: bool = False,
) -> float | None:
    """Cost of one call, or ``None`` when the model has no configured price."""
    table = DEFAULT_PRICING if pricing is None else pricing
    price = resolve_price(model, table)
    return None if price is None else price.cost(usage, off_peak=off_peak)


@dataclass
class UsageLedger:
    """Accumulates tokens and spend across calls, optionally enforcing a ceiling.

    The ceiling is checked *after* each call, since a call's cost is unknowable
    beforehand; the run therefore stops at the first call that crosses the limit.
    """

    limit: float | None = None
    pricing: dict[str, ModelPrice] = field(default_factory=build_pricing)
    currency: str = DEFAULT_CURRENCY
    strict_pricing: bool = False
    # ``None`` means "ask the clock"; a bool pins the period (tests, batch jobs).
    off_peak: bool | None = None
    clock: Callable[[], datetime] | None = None
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    peak_calls: int = 0
    off_peak_calls: int = 0
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

    def resolve_period(self) -> bool:
        """True when the next call should be billed at the off-peak rate."""
        if self.off_peak is not None:
            return self.off_peak
        moment = None if self.clock is None else self.clock()
        return billing_period(moment) == OFF_PEAK

    def record(self, model: str, usage: Usage, *, off_peak: bool | None = None) -> float:
        """Record one call and return its cost.

        Raise :class:`UnknownModelPriceError` for an unpriced model unless
        ``strict_pricing`` was disabled, so unmetered spend can never be invisible.
        """
        is_off_peak = self.resolve_period() if off_peak is None else off_peak
        price = resolve_price(model, self.pricing)
        if price is not None and price.currency != self.currency:
            raise ValueError(
                f"价目货币不一致：账本记的是 {self.currency}，{model!r} 的价目是 "
                f"{price.currency}。元与美元混在一张表里算出来的总额没有意义。"
            )
        if price is None:
            self.unpriced_models.add(model)
            call_cost = 0.0
        else:
            call_cost = price.cost(usage, off_peak=is_off_peak)
        self.calls += 1
        if is_off_peak:
            self.off_peak_calls += 1
        else:
            self.peak_calls += 1
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.cost += call_cost
        if price is None and (self.strict_pricing or self.limit is not None):
            raise UnknownModelPriceError(
                f"模型 {model!r} 没有价目，无法计入成本"
                f"{'，预算上限形同虚设' if self.limit is not None else ''}。"
                f"请在 pricing 中补充该模型价格"
                f"（可用 AGENTEVALOPS_PRICING_JSON 在 .env 里声明），"
                f"或设置 strict_pricing=False 并接受成本只算下限。"
            )
        return call_cost

    def assert_within_budget(self) -> None:
        if self.limit is not None and self.cost >= self.limit:
            raise BudgetExceededError(
                f"本次运行已花费 {self.currency} {self.cost:.6f}，"
                f"达到或超过预算上限 {self.limit:.6f}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost": self.cost,
            "currency": self.currency,
            "limit": self.limit,
            "peak_calls": self.peak_calls,
            "off_peak_calls": self.off_peak_calls,
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
        extra_body: Mapping[str, Any] | None = None,
    ) -> LLMResponse:
        # 只在调用方显式给了 extra_body 时才转发：测试里的 stub 保持旧签名也能用。
        response = self.client.complete(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **({"extra_body": extra_body} if extra_body else {}),
        )
        self.ledger.record(response.model, response.usage)
        self.ledger.assert_within_budget()
        return response
