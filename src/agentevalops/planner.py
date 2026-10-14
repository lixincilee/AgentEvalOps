"""Intent-and-slot planning: the one layer the rules/LLM swap touches.

``RulePlanner`` is v1 (deterministic). ``LLMPlanner`` is v2: it asks the model, but
grounds every slot against the raw message and falls back to v1 on any failure. Both
return the same :class:`Plan`, so ``AuditRecord`` -- and therefore every existing
test -- keeps its meaning while v1 and v2 are compared.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .llm.base import ChatMessage, LLMClient, LLMError, LLMResponse
from .models import Intent

PROMPT_VERSION = "v2"
# The word boundaries are load-bearing: without them "ORD-10011" silently resolves to
# ORD-1001 and the customer gets an answer about somebody else's order.
ORDER_ID_PATTERN = re.compile(r"\bORD-\d{4}\b", re.IGNORECASE)

SOURCE_RULES = "rules"
SOURCE_LLM = "llm"
SOURCE_FALLBACK = "llm-fallback"

SYSTEM_PROMPT = (
    "你是电商客服系统的意图识别模块。只输出一个 JSON 对象，不要输出任何解释或代码块标记。\n"
    "字段：\n"
    '- "intent"：order_status / refund / human_handoff / unknown 之一\n'
    '- "order_id"：用户消息中出现的订单号，形如 ORD-1001；没有就填 null\n'
    "规则：order_id 必须原样出现在用户消息里，禁止编造；无法判断意图时填 unknown。"
)

INTENT_ALIASES: dict[str, Intent] = {
    "order_status": Intent.ORDER_STATUS,
    "status": Intent.ORDER_STATUS,
    "refund": Intent.REFUND,
    "human_handoff": Intent.HUMAN_HANDOFF,
    "human": Intent.HUMAN_HANDOFF,
    "escalate": Intent.HUMAN_HANDOFF,
    "unknown": Intent.UNKNOWN,
}


def classify_intent(message: str) -> Intent:
    """v1 rule classifier -- the baseline every model version is measured against."""
    text = message.casefold()
    if any(word in text for word in ("退款", "退货", "refund")):
        return Intent.REFUND
    if any(word in text for word in ("人工", "客服", "投诉", "human", "agent")):
        return Intent.HUMAN_HANDOFF
    status_word = any(
        word in text for word in ("状态", "物流", "到哪", "查询", "status", "tracking")
    )
    order_word = any(word in text for word in ("订单", "order", "ord-"))
    if status_word and order_word:
        return Intent.ORDER_STATUS
    return Intent.UNKNOWN


def extract_order_id(message: str) -> str | None:
    match = ORDER_ID_PATTERN.search(message)
    return match.group(0).upper() if match else None


@dataclass(frozen=True)
class Plan:
    """One decision: what the user wants, which order it concerns, and who decided."""

    intent: Intent
    order_id: str | None
    source: str
    note: str | None = None


@runtime_checkable
class Planner(Protocol):
    @property
    def name(self) -> str: ...

    def plan(self, message: str) -> Plan: ...


class RulePlanner:
    """v1: pure string matching, zero cost, zero variance."""

    name = "rules-v1"

    def plan(self, message: str) -> Plan:
        return Plan(classify_intent(message), extract_order_id(message), SOURCE_RULES)


def parse_plan_json(text: str) -> dict[str, Any] | None:
    """Pull one JSON object out of a reply; models like to wrap it in prose or fences."""
    if not text:
        return None
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def coerce_intent(value: Any) -> Intent | None:
    if not isinstance(value, str):
        return None
    return INTENT_ALIASES.get(value.strip().casefold())


def ground_order_id(value: Any, message: str) -> tuple[str | None, str | None]:
    """Only accept an order id the user actually wrote; never trust a made-up one.

    Returns ``(order_id, note)``; the note explains every silent correction so the
    trace can show *why* the executed slot differs from what the model proposed.
    """
    from_message = extract_order_id(message)
    if value is None or (isinstance(value, str) and not value.strip()):
        note = None if from_message is None else "模型未给出订单号，已按原文补全"
        return from_message, note
    if not isinstance(value, str):
        return from_message, "模型给出的订单号类型非法，已按原文抽取"
    candidate = value.strip().upper()
    if not ORDER_ID_PATTERN.fullmatch(candidate):
        return from_message, f"模型给出的订单号格式非法（{value!r}），已按原文抽取"
    if candidate not in message.upper():
        return from_message, f"模型编造了订单号 {candidate}，已按原文抽取"
    return candidate, None


class LLMPlanner:
    """v2: ask the model, but never let a bad answer reach the tools."""

    def __init__(
        self,
        client: LLMClient,
        *,
        fallback: RulePlanner | None = None,
        max_tokens: int = 128,
    ) -> None:
        self.client = client
        self.fallback = fallback or RulePlanner()
        self.max_tokens = max_tokens
        self.last_response: LLMResponse | None = None

    @property
    def name(self) -> str:
        return f"llm-{PROMPT_VERSION}"

    def plan(self, message: str) -> Plan:
        self.last_response = None
        try:
            response = self.client.complete(
                [ChatMessage.system(SYSTEM_PROMPT), ChatMessage.user(message)],
                max_tokens=self.max_tokens,
            )
        except LLMError as exc:
            return self._fallback(message, f"模型调用失败（{type(exc).__name__}）")
        self.last_response = response
        payload = parse_plan_json(response.text)
        if payload is None:
            return self._fallback(message, "模型输出不是合法 JSON 对象")
        intent = coerce_intent(payload.get("intent"))
        if intent is None:
            return self._fallback(
                message, f"模型给出的 intent 非法（{payload.get('intent')!r}）"
            )
        order_id, note = ground_order_id(payload.get("order_id"), message)
        return Plan(intent, order_id, SOURCE_LLM, note)

    def _fallback(self, message: str, note: str) -> Plan:
        plan = self.fallback.plan(message)
        return Plan(plan.intent, plan.order_id, SOURCE_FALLBACK, note)
