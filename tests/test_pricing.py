"""Price-table parsing, alias honesty, and client wiring."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from agentevalops.config import Settings
from agentevalops.llm import (
    ChatMessage,
    MeteredLLMClient,
    ModelPrice,
    Usage,
    UsageLedger,
    approx_tokens,
    billing_period,
    build_client,
    build_pricing,
    estimate_cost,
    pricing_from_json,
)

FLASH_TABLE = '{"scripted-fake": {"input_per_1m": 1.0, "output_per_1m": 1.0}}'


def test_pricing_from_json_parses_a_minimal_table():
    payload = json.dumps({"m": {"input_per_1m": 1.0, "output_per_1m": 2.0}})
    assert pricing_from_json(payload) == {"m": ModelPrice(1.0, 2.0, None)}


def test_pricing_from_json_accepts_an_optional_cache_hit_rate():
    payload = json.dumps(
        {
            "m": {
                "input_per_1m": 1.0,
                "output_per_1m": 2.0,
                "cache_hit_input_per_1m": 0.1,
            }
        }
    )
    assert pricing_from_json(payload)["m"].cache_hit_input_per_1m == 0.1


def test_pricing_from_json_accepts_an_empty_object():
    assert pricing_from_json("{}") == {}


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        "[1, 2, 3]",
        json.dumps({"m": 5}),
        json.dumps({"m": {"input_per_1m": 1.0}}),
        json.dumps({"m": {"input_per_1m": "abc", "output_per_1m": 2.0}}),
    ],
)
def test_pricing_from_json_rejects_malformed_tables(payload):
    with pytest.raises(ValueError):
        pricing_from_json(payload)


def test_the_table_covers_exactly_the_models_this_endpoint_serves():
    """deepseek-chat 会被别名解析成 deepseek-flash 计费，所以只能给它「未定价」。"""
    table = build_pricing()
    assert "deepseek-flash" in table
    assert "deepseek-v4-pro" in table
    assert "deepseek-chat" not in table
    assert estimate_cost("deepseek-chat", Usage(1_000_000, 1_000_000), table) is None


def test_build_client_stays_unmetered_without_a_budget_or_overrides():
    client = build_client(Settings(), offline=True, scripts=["ok"])
    assert not isinstance(client, MeteredLLMClient)


def test_build_client_meters_when_price_overrides_are_declared():
    client = build_client(
        Settings(pricing_json=FLASH_TABLE), offline=True, scripts=["ok"]
    )
    assert isinstance(client, MeteredLLMClient)
    client.complete([ChatMessage.user("hi")])
    assert client.ledger.cost_is_complete is True
    assert client.ledger.cost > 0


def test_build_client_meters_when_a_budget_ceiling_is_declared():
    client = build_client(Settings(budget=10.0), offline=True, scripts=["ok"])
    assert isinstance(client, MeteredLLMClient)
    assert client.ledger.limit == 10.0


def test_ledger_tokens_come_from_the_client_not_from_the_price_table():
    client = build_client(
        Settings(pricing_json=FLASH_TABLE), offline=True, scripts=["ok"]
    )
    client.complete([ChatMessage.user("hello")])
    assert client.ledger.prompt_tokens == approx_tokens("hello")


def test_the_published_prices_are_reproduced_exactly():
    """价目来自供应商官方价目页（人民币元/百万 tokens），逐条钉死防止改错。"""
    table = build_pricing()
    flash = table["deepseek-flash"]
    assert flash.cost(Usage(1_000_000, 1_000_000)) == pytest.approx(2.0 + 8.0)
    assert flash.cost(Usage(1_000_000, 1_000_000), off_peak=True) == pytest.approx(1.0 + 4.0)
    assert flash.cost(Usage(1_000_000, 0, cache_hit_tokens=1_000_000)) == pytest.approx(0.04)
    pro = table["deepseek-v4-pro"]
    assert pro.cost(Usage(1_000_000, 0)) == pytest.approx(9.0)
    assert pro.cost(Usage(1_000_000, 0), off_peak=True) == pytest.approx(4.5)
    assert pro.cost(Usage(0, 1_000_000), off_peak=True) == pytest.approx(13.5)


def test_prices_are_quoted_in_yuan():
    """供应商以元结算；把它当成美元会让成本报告虚低约 7 倍。"""
    assert build_pricing()["deepseek-flash"].currency == "CNY"


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 10, 14, 10, 30), "peak"),      # 周三上午
        (datetime(2026, 10, 14, 12, 30), "off_peak"),  # 午休
        (datetime(2026, 10, 14, 20, 30), "off_peak"),  # 周三晚上
        (datetime(2026, 10, 17, 10, 30), "off_peak"),  # 周六
    ],
)
def test_billing_period_follows_beijing_peak_hours(moment, expected):
    assert billing_period(moment) == expected


def test_the_ledger_can_pin_the_period_for_reproducible_reports():
    ledger = UsageLedger(pricing=build_pricing(), off_peak=True)
    ledger.record("deepseek-flash", Usage(1_000_000, 0))
    assert ledger.cost == pytest.approx(1.0)
    assert (ledger.peak_calls, ledger.off_peak_calls) == (0, 1)


def test_mixing_currencies_in_one_ledger_is_refused():
    """元与美元混着加总，得到的总额没有任何意义，所以要当场报错。"""
    ledger = UsageLedger(pricing={"usd-model": ModelPrice(1.0, 1.0, currency="USD")})
    with pytest.raises(ValueError, match="货币不一致"):
        ledger.record("usd-model", Usage(1, 1))


def test_pricing_json_inherits_the_configured_currency():
    table = pricing_from_json(
        '{"m": {"input_per_1m": 1, "output_per_1m": 1}}', default_currency="USD"
    )
    assert table["m"].currency == "USD"


@pytest.mark.parametrize("pinned_period", [None, True, False])
def test_the_ledger_call_counters_always_reconcile(pinned_period):
    """每次调用都恰好落进「高峰」或「空闲」中的一档，不会两边都不记。

    这个不变式曾经对不上：报告里 165 条用例只统计出 159 次调用，原因是 6 条用例在
    规划前就被拒绝、根本没发起调用，而报告没有说明这一点。
    """
    ledger = UsageLedger(pricing=build_pricing(), off_peak=pinned_period)
    for _ in range(7):
        ledger.record("deepseek-flash", Usage(1_000, 100))
    assert ledger.calls == 7
    assert ledger.peak_calls + ledger.off_peak_calls == ledger.calls


def test_unpriced_calls_are_still_counted_as_calls():
    """算不出成本不等于没发生：漏计调用会让「调用次数」这类口径直接失真。"""
    ledger = UsageLedger(pricing={})
    ledger.record("mystery-model", Usage(10, 5))
    assert ledger.calls == 1
    assert ledger.peak_calls + ledger.off_peak_calls == 1
    assert ledger.cost == 0.0
    assert ledger.cost_is_complete is False
