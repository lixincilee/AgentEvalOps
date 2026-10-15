"""Pricing, spend accounting, and budget enforcement."""

from __future__ import annotations

import pytest

from agentevalops.llm import (
    BudgetExceededError,
    ChatMessage,
    MeteredLLMClient,
    ModelPrice,
    ScriptedLLMClient,
    UnknownModelPriceError,
    Usage,
    UsageLedger,
    build_pricing,
    estimate_cost,
    resolve_price,
)

PRICE = ModelPrice(input_per_1m=1.0, output_per_1m=2.0)


def test_model_price_math_is_exact():
    assert PRICE.cost(Usage(1_000_000, 0)) == 1.0
    assert PRICE.cost(Usage(0, 1_000_000)) == 2.0
    assert PRICE.cost(Usage(500_000, 250_000)) == 1.0


def test_model_price_handles_zero_usage():
    assert PRICE.cost(Usage()) == 0.0


def test_cache_hit_tokens_are_billed_at_their_own_rate():
    price = ModelPrice(
        input_per_1m=10.0,
        output_per_1m=100.0,
        cache_hit_input_per_1m=1.0,
    )
    # 1M prompt tokens, half of them cache hits: 0.5M * $10 + 0.5M * $1 = $5.50
    assert price.cost(Usage(1_000_000, 0, cache_hit_tokens=500_000)) == pytest.approx(5.5)


def test_cache_hits_fall_back_to_the_normal_input_rate_when_unpriced():
    assert PRICE.cost(Usage(1_000_000, 0, cache_hit_tokens=500_000)) == pytest.approx(1.0)


def test_reported_cache_hits_cannot_exceed_the_prompt_tokens_actually_billed():
    """A provider bug reporting more hits than prompt tokens must not create credit."""
    price = ModelPrice(1.0, 0.0, cache_hit_input_per_1m=0.0)
    assert price.cost(Usage(1_000, 0, cache_hit_tokens=5_000)) == 0.0


def test_usage_addition_carries_cache_hit_tokens():
    total = Usage(10, 5, cache_hit_tokens=4) + Usage(1, 2, cache_hit_tokens=6)
    assert total.cache_hit_tokens == 10
    assert total.total_tokens == 18


def test_estimate_cost_returns_none_for_an_unpriced_model():
    assert estimate_cost("some-unknown-model", Usage(10, 10)) is None


def test_estimate_cost_uses_the_injected_table():
    assert estimate_cost("demo", Usage(1_000_000, 0), {"demo": PRICE}) == 1.0


def test_build_pricing_merges_overrides_without_dropping_defaults():
    merged = build_pricing({"custom": PRICE})
    assert merged["custom"].input_per_1m == 1.0
    assert "deepseek-flash" in merged


def test_ledger_accumulates_tokens_and_cost():
    ledger = UsageLedger(pricing={"m": PRICE}, off_peak=False)
    ledger.record("m", Usage(1_000_000, 1_000_000))
    ledger.record("m", Usage(0, 1_000_000))
    assert ledger.calls == 2
    assert ledger.prompt_tokens == 1_000_000
    assert ledger.completion_tokens == 2_000_000
    assert ledger.total_tokens == 3_000_000
    # (1M in * 1 + 1M out * 2) + (1M out * 2) = 5（单位随价目表，这里是无单位数字）
    assert ledger.cost == 5.0


def test_unpriced_models_are_flagged_rather_than_billed_as_free():
    """Default mode records the call but refuses to call the total complete."""
    ledger = UsageLedger(pricing={})
    assert ledger.record("mystery", Usage(100, 100)) == 0.0
    assert ledger.calls == 1
    assert ledger.unpriced_models == {"mystery"}
    assert ledger.all_calls_priced is False
    assert ledger.cost_is_complete is False
    assert ledger.to_dict()["unpriced_models"] == ["mystery"]
    assert ledger.to_dict()["strict_pricing"] is False


def test_unpriced_models_raise_when_strict_pricing_is_on():
    ledger = UsageLedger(pricing={}, strict_pricing=True)
    with pytest.raises(UnknownModelPriceError):
        ledger.record("mystery", Usage(100, 100))
    assert ledger.unpriced_models == {"mystery"}


def test_unpriced_models_raise_when_a_budget_ceiling_is_set():
    """A ceiling is meaningless if unmetered calls slip under it as $0.00."""
    ledger = UsageLedger(limit=1.0, pricing={})
    with pytest.raises(UnknownModelPriceError):
        ledger.record("mystery", Usage(100, 100))


def test_priced_ledger_reports_a_complete_cost():
    ledger = UsageLedger(pricing={"m": PRICE})
    ledger.record("m", Usage(1_000_000, 0))
    assert ledger.cost_is_complete is True
    assert ledger.to_dict()["cost_is_complete"] is True


def test_versioned_model_names_resolve_by_longest_prefix():
    pricing = build_pricing()
    assert resolve_price("deepseek-flash-v4-1", pricing) is pricing["deepseek-flash"]
    assert estimate_cost("deepseek-flash-v4-1", Usage(1_000_000, 0)) == pytest.approx(2.0)
    assert resolve_price("totally-unknown-model", pricing) is None


def test_ledger_has_no_ceiling_by_default():
    ledger = UsageLedger(pricing={"m": PRICE})
    ledger.record("m", Usage(1_000_000, 1_000_000))
    ledger.assert_within_budget()


def test_ledger_trips_when_spend_reaches_the_ceiling():
    ledger = UsageLedger(limit=1.0, pricing={"m": PRICE}, off_peak=False)
    ledger.record("m", Usage(500_000, 0))
    ledger.assert_within_budget()
    ledger.record("m", Usage(500_000, 0))
    with pytest.raises(BudgetExceededError):
        ledger.assert_within_budget()


def test_ledger_trips_when_spend_exceeds_the_ceiling():
    ledger = UsageLedger(limit=1.0, pricing={"m": PRICE}, off_peak=False)
    ledger.record("m", Usage(2_000_000, 0))
    with pytest.raises(BudgetExceededError):
        ledger.assert_within_budget()


def test_off_peak_hours_are_billed_at_half_the_peak_price():
    """空闲时段是高峰价的一半，且两类调用分开计数。

    时段必须由参数钉死，不能靠墙上时钟：北京时间高峰是周一至周五 9-12 点与 14-18 点，
    断言若依赖当前时刻，同一份代码上午跑绿、午休跑红（本仓库真发生过）。
    """
    peak = UsageLedger(pricing={"m": PRICE}, off_peak=False)
    off = UsageLedger(pricing={"m": PRICE}, off_peak=True)
    for ledger in (peak, off):
        ledger.record("m", Usage(1_000_000, 1_000_000))
    assert peak.cost == 3.0
    assert off.cost == 1.5
    assert (peak.peak_calls, peak.off_peak_calls) == (1, 0)
    assert (off.peak_calls, off.off_peak_calls) == (0, 1)


def test_metered_client_records_usage_from_the_inner_client():
    metered = MeteredLLMClient(
        ScriptedLLMClient(["hello"], model="m"), UsageLedger(pricing={"m": PRICE})
    )
    metered.complete([ChatMessage.user("hi")])
    assert metered.ledger.calls == 1
    assert metered.ledger.total_tokens > 0
    assert metered.ledger.cost > 0


def test_metered_client_delegates_model_id():
    metered = MeteredLLMClient(ScriptedLLMClient(model="m"), UsageLedger())
    assert metered.model_id == "m"


def test_metered_client_stops_the_run_when_over_budget():
    metered = MeteredLLMClient(
        ScriptedLLMClient(["a", "b"], model="m"),
        UsageLedger(limit=0.0, pricing={"m": PRICE}),
    )
    with pytest.raises(BudgetExceededError):
        metered.complete([ChatMessage.user("hi")])
