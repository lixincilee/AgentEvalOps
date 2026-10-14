"""Price-table parsing, alias honesty, and client wiring."""

from __future__ import annotations

import json

import pytest

from agentevalops.config import Settings
from agentevalops.llm import (
    ChatMessage,
    MeteredLLMClient,
    ModelPrice,
    Usage,
    approx_tokens,
    build_client,
    build_pricing,
    estimate_cost,
    pricing_from_json,
)

FLASH_TABLE = '{"scripted-fake": {"input_per_1m_usd": 1.0, "output_per_1m_usd": 1.0}}'


def test_pricing_from_json_parses_a_minimal_table():
    payload = json.dumps({"m": {"input_per_1m_usd": 1.0, "output_per_1m_usd": 2.0}})
    assert pricing_from_json(payload) == {"m": ModelPrice(1.0, 2.0, None)}


def test_pricing_from_json_accepts_an_optional_cache_hit_rate():
    payload = json.dumps(
        {
            "m": {
                "input_per_1m_usd": 1.0,
                "output_per_1m_usd": 2.0,
                "cache_hit_input_per_1m_usd": 0.1,
            }
        }
    )
    assert pricing_from_json(payload)["m"].cache_hit_input_per_1m_usd == 0.1


def test_pricing_from_json_accepts_an_empty_object():
    assert pricing_from_json("{}") == {}


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        "[1, 2, 3]",
        json.dumps({"m": 5}),
        json.dumps({"m": {"input_per_1m_usd": 1.0}}),
        json.dumps({"m": {"input_per_1m_usd": "abc", "output_per_1m_usd": 2.0}}),
    ],
)
def test_pricing_from_json_rejects_malformed_tables(payload):
    with pytest.raises(ValueError):
        pricing_from_json(payload)


def test_alias_models_are_not_silently_priced():
    """The endpoint's real ids stay unpriced until declared; guessing is forbidden."""
    table = build_pricing()
    assert "deepseek-chat" in table
    assert "deepseek-flash" not in table
    assert estimate_cost("deepseek-flash", Usage(1_000_000, 1_000_000), table) is None


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
    assert client.ledger.cost_usd > 0


def test_build_client_meters_when_a_budget_ceiling_is_declared():
    client = build_client(Settings(budget_usd=10.0), offline=True, scripts=["ok"])
    assert isinstance(client, MeteredLLMClient)
    assert client.ledger.limit_usd == 10.0


def test_ledger_tokens_come_from_the_client_not_from_the_price_table():
    client = build_client(
        Settings(pricing_json=FLASH_TABLE), offline=True, scripts=["ok"]
    )
    client.complete([ChatMessage.user("hello")])
    assert client.ledger.prompt_tokens == approx_tokens("hello")
