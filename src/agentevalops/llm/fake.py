"""Deterministic, network-free client used by the offline test suite."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .base import LLMError, LLMResponse, Usage, approx_tokens
from .base import ChatMessage


class ScriptExhaustedError(LLMError):
    """The script ran out of canned answers for an unscripted prompt."""


class ScriptedLLMClient:
    """Replays canned answers so tests stay free, offline, and reproducible.

    Selection order for a call:

    1. the next entry of ``responses`` (strict call order), else
    2. the first ``by_key`` substring found in the last user message, else
    3. ``default`` if given, else raise :class:`ScriptExhaustedError`.

    ``latency_ms`` is always ``0.0`` and token counts are approximated, so results
    are byte-for-byte stable across runs.
    """

    def __init__(
        self,
        responses: Sequence[str] = (),
        *,
        by_key: Mapping[str, str] | None = None,
        default: str | None = None,
        model: str = "scripted-fake",
    ) -> None:
        self._ordered = list(responses)
        self._by_key = dict(by_key or {})
        self._default = default
        self._model = model
        self._cursor = 0
        self.calls: list[list[ChatMessage]] = []

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_prompt(self) -> str:
        if not self.calls:
            return ""
        return "\n".join(message.content for message in self.calls[-1])

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        recorded = list(messages)
        self.calls.append(recorded)
        text = self._select(recorded)
        return LLMResponse(
            text=text,
            model=self._model,
            requested_model=self._model,
            usage=Usage(
                prompt_tokens=sum(approx_tokens(m.content) for m in recorded),
                completion_tokens=approx_tokens(text),
            ),
            latency_ms=0.0,
        )

    def _select(self, messages: list[ChatMessage]) -> str:
        if self._cursor < len(self._ordered):
            text = self._ordered[self._cursor]
            self._cursor += 1
            return text
        last_user = next(
            (m.content for m in reversed(messages) if m.role == "user"), ""
        )
        for key, text in self._by_key.items():
            if key in last_user:
                return text
        if self._default is not None:
            return self._default
        raise ScriptExhaustedError(
            f"脚本已用尽（第 {self._cursor + 1} 次调用），且没有匹配的 by_key/default。"
            f"最后一条用户消息: {last_user[:200]!r}"
        )
