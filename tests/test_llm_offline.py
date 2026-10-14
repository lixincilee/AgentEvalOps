"""Offline LLM contracts: message shapes and the scripted client."""

from __future__ import annotations

import json

import pytest

from agentevalops.llm import (
    ChatMessage,
    LLMClient,
    LLMResponse,
    ScriptExhaustedError,
    ScriptedLLMClient,
    Usage,
    approx_tokens,
)


def test_chat_message_convenience_constructors():
    assert ChatMessage.system("s").role == "system"
    assert ChatMessage.assistant("a").role == "assistant"
    assert ChatMessage.user("u").to_dict() == {"role": "user", "content": "u"}


def test_usage_addition_and_totals():
    total = Usage(10, 5) + Usage(1, 2)
    assert (total.prompt_tokens, total.completion_tokens) == (11, 7)
    assert total.total_tokens == 18
    assert total.to_dict() == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "cache_hit_tokens": 0,
        "total_tokens": 18,
    }


def test_llm_response_is_json_serializable():
    response = LLMResponse(text="hi", model="m", usage=Usage(3, 4), latency_ms=1.5)
    assert json.loads(json.dumps(response.to_dict()))["usage"]["total_tokens"] == 7


def test_llm_response_keeps_both_requested_and_served_model_ids():
    """Provider aliases mean the served model can differ from the requested one."""
    response = LLMResponse(
        text="ok", model="deepseek-flash", requested_model="deepseek-chat"
    )
    payload = response.to_dict()
    assert payload["model"] == "deepseek-flash"
    assert payload["requested_model"] == "deepseek-chat"


def test_scripted_client_replays_in_call_order():
    client = ScriptedLLMClient(["first", "second"])
    assert client.complete([ChatMessage.user("a")]).text == "first"
    assert client.complete([ChatMessage.user("b")]).text == "second"
    assert client.call_count == 2


def test_scripted_client_selects_by_key_substring():
    client = ScriptedLLMClient(by_key={"退款": "refund-answer", "物流": "status-answer"})
    assert client.complete([ChatMessage.user("我要退款")]).text == "refund-answer"
    assert client.complete([ChatMessage.user("帮我查物流")]).text == "status-answer"


def test_scripted_client_uses_default_before_failing():
    client = ScriptedLLMClient(default="fallback")
    assert client.complete([ChatMessage.user("anything")]).text == "fallback"


def test_scripted_client_raises_a_helpful_error_when_exhausted():
    client = ScriptedLLMClient()
    with pytest.raises(ScriptExhaustedError) as excinfo:
        client.complete([ChatMessage.user("你好")])
    assert "你好" in str(excinfo.value)


def test_scripted_client_is_byte_for_byte_deterministic():
    def run() -> tuple[dict, int]:
        client = ScriptedLLMClient(["x"], by_key={"k": "v"})
        return client.complete([ChatMessage.user("k")]).to_dict(), client.call_count

    assert run() == run()


def test_scripted_client_reports_zero_latency_and_approximated_usage():
    client = ScriptedLLMClient(["a" * 40])
    response = client.complete([ChatMessage.user("b" * 20)])
    assert response.latency_ms == 0.0
    assert response.usage.prompt_tokens == approx_tokens("b" * 20)
    assert response.usage.completion_tokens == approx_tokens("a" * 40)


def test_scripted_client_records_every_request():
    client = ScriptedLLMClient(["ok"])
    client.complete([ChatMessage.system("sys"), ChatMessage.user("问题")])
    assert client.last_prompt == "sys\n问题"


def test_scripted_client_satisfies_the_client_protocol():
    assert isinstance(ScriptedLLMClient(["ok"]), LLMClient)


def test_approx_tokens_edge_cases():
    assert approx_tokens("") == 0
    assert approx_tokens("abcd") == 1


def test_scripted_client_stamps_the_requested_model():
    response = ScriptedLLMClient(["ok"], model="m").complete([ChatMessage.user("hi")])
    assert response.requested_model == "m"
