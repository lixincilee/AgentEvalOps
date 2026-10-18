"""DeepSeek adapter behaviour, exercised with a stub provider (never any network)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agentevalops.llm import ChatMessage, LLMUnavailableError, MissingCredentialError
from agentevalops.llm.deepseek import DeepSeekClient


def _completion(
    text: str = "hi", *, model: str = "deepseek-chat", prompt: int = 7, completion: int = 3
) -> SimpleNamespace:
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


class StubProvider:
    """Mimics the small slice of the openai SDK that the adapter touches."""

    def __init__(self, *, response: Any = None, error: Exception | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self._response = response
        self._error = error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


def test_blank_api_key_is_rejected():
    with pytest.raises(MissingCredentialError):
        DeepSeekClient(api_key="   ")


def test_returns_text_usage_and_model():
    client = DeepSeekClient(api_key="sk-test", client=StubProvider(response=_completion("已发货")))
    response = client.complete([ChatMessage.user("查订单")])
    assert response.text == "已发货"
    assert response.model == "deepseek-chat"
    assert response.usage.prompt_tokens == 7
    assert response.usage.completion_tokens == 3
    assert isinstance(response.latency_ms, float)


def test_sends_model_messages_and_default_params():
    provider = StubProvider(response=_completion())
    client = DeepSeekClient(
        api_key="sk-test", temperature=0.0, max_tokens=256, client=provider
    )
    client.complete([ChatMessage.system("sys"), ChatMessage.user("u")])
    request = provider.requests[0]
    assert request["model"] == "deepseek-chat"
    assert request["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u"},
    ]
    assert request["temperature"] == 0.0
    assert request["max_tokens"] == 256


def test_per_call_overrides_win_over_defaults():
    provider = StubProvider(response=_completion())
    client = DeepSeekClient(
        api_key="sk-test", temperature=0.0, max_tokens=256, client=provider
    )
    client.complete([ChatMessage.user("u")], temperature=0.7, max_tokens=32)
    assert provider.requests[0]["temperature"] == 0.7
    assert provider.requests[0]["max_tokens"] == 32


def test_extra_body_is_forwarded_only_when_given():
    """判分层靠 extra_body 关掉 DeepSeek 思考模式；不能悄悄加给所有调用。"""
    provider = StubProvider(response=_completion())
    client = DeepSeekClient(api_key="sk-test", client=provider)
    client.complete(
        [ChatMessage.user("u")], extra_body={"thinking": {"type": "disabled"}}
    )
    assert provider.requests[0]["extra_body"] == {"thinking": {"type": "disabled"}}

    provider.requests.clear()
    client.complete([ChatMessage.user("u")])
    assert "extra_body" not in provider.requests[0]


def test_provider_failures_are_wrapped_as_llm_unavailable():
    client = DeepSeekClient(
        api_key="sk-test", client=StubProvider(error=RuntimeError("429 Too Many Requests"))
    )
    with pytest.raises(LLMUnavailableError) as excinfo:
        client.complete([ChatMessage.user("u")])
    assert "429" in str(excinfo.value)


def test_missing_provider_fields_degrade_gracefully():
    provider = StubProvider(response=SimpleNamespace(choices=[], usage=None))
    client = DeepSeekClient(api_key="sk-test", client=provider)
    response = client.complete([ChatMessage.user("u")])
    assert response.text == ""
    assert response.usage.prompt_tokens == 0
    assert response.model == "deepseek-chat"


def test_our_own_error_types_are_not_double_wrapped():
    client = DeepSeekClient(
        api_key="sk-test", client=StubProvider(error=MissingCredentialError("内部错误"))
    )
    with pytest.raises(MissingCredentialError):
        client.complete([ChatMessage.user("u")])


def test_model_id_reflects_the_configured_model():
    client = DeepSeekClient(
        api_key="sk-test", model="deepseek-reasoner", client=StubProvider()
    )
    assert client.model_id == "deepseek-reasoner"
