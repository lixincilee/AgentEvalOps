"""Multi-turn session state: slot inheritance with an honest source label.

SPEC-MT-2 / 3 / 4 / 6. The module exists for one reason: a slot the user typed and a slot
we carried over from the previous turn carry different risk, and collapsing the two makes
every later failure unattributable -- was the order id misheard, or did state leak?
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import SLOT_INHERITED, SLOT_VERBATIM, Intent
from .planner import extract_order_id

#: 只有这三种意图消费订单号槽位。policy_qa / unknown 不参与继承（SPEC-MT-3 的延伸）：
#: 它们本来就不用订单号，继承过来只会污染会话；会话状态越小，串味的面积越小。
SLOT_INTENTS: frozenset[Intent] = frozenset(
    {Intent.ORDER_STATUS, Intent.REFUND, Intent.HUMAN_HANDOFF}
)


@dataclass
class Turn:
    """一轮已经发生的对话。槽位来源与值分开存，来源不会被谁顺手丢掉。"""

    message: str
    intent: Intent
    order_id: str | None = None
    slot_source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "intent": str(self.intent),
            "order_id": self.order_id,
            "slot_source": self.slot_source,
        }


@dataclass
class SessionState:
    """一个 (customer_id, session_id) 对应的会话状态。"""

    session_id: str
    customer_id: str
    turns: list[Turn] = field(default_factory=list)
    last_order_id: str | None = None
    last_intent: Intent | None = None

    def remember(
        self,
        message: str,
        intent: Intent,
        order_id: str | None = None,
        slot_source: str | None = None,
    ) -> Turn:
        """记下一轮。**意图与槽位分开记**：意图只用于排查，绝不用于继承。"""
        turn = Turn(message, intent, order_id, slot_source)
        self.turns.append(turn)
        if intent in SLOT_INTENTS and order_id:
            self.last_order_id = order_id
        self.last_intent = intent
        return turn

    def hint(self) -> str | None:
        """给规划器的会话提示：只说槽位，不说意图（SPEC-MT-3）。

        提示是**给模型理解指代用的**，不是让模型照着填槽位：槽位最后仍走
        ``resolve_slot``，由它决定标 verbatim 还是 inherited。
        """
        if self.last_order_id is None:
            return None
        return (
            "会话上下文：上一轮提到的订单号是 "
            f"{self.last_order_id}。只有在用户本轮自己没给出订单号时才参考它，"
            "禁止据此在 order_id 字段里编造订单号。"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "customer_id": self.customer_id,
            "last_order_id": self.last_order_id,
            "last_intent": None if self.last_intent is None else str(self.last_intent),
            "turns": [turn.to_dict() for turn in self.turns],
        }


class SessionStore:
    """会话状态容器。键里必须同时有 customer_id（SPEC-MT-6）。

    只按 session_id 存是这类系统最常见的越权来源：另一个客户拿着同一个
    session_id 就能继承上一个客户的订单号。
    """

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], SessionState] = {}

    def state_for(self, session_id: str, customer_id: str) -> SessionState:
        key = (session_id, customer_id)
        state = self._sessions.get(key)
        if state is None:
            state = SessionState(session_id=session_id, customer_id=customer_id)
            self._sessions[key] = state
        return state

    def get(self, session_id: str, customer_id: str) -> SessionState | None:
        return self._sessions.get((session_id, customer_id))

    def clear(self) -> None:
        self._sessions.clear()

    def __len__(self) -> int:
        return len(self._sessions)


def resolve_slot(
    message: str,
    *,
    planned: str | None = None,
    previous_order_id: str | None = None,
) -> tuple[str | None, str | None]:
    """返回 ``(order_id, source)``：原文优先，其次继承，都没有就是缺槽位。

    继承**只在消息本身没有订单号时**发生（SPEC-MT-2），所以继承值永远不可能覆盖
    用户本轮明说的值。指代词（SPEC-MT-4）走的就是「本轮没有订单号」这条路：
    有上一轮订单号就指向它，没有就返回 ``(None, None)`` 交给 SPEC-STATE-1 报缺槽位，
    **不猜**。
    """
    verbatim = planned if planned is not None else extract_order_id(message)
    if verbatim:
        return verbatim, SLOT_VERBATIM
    if previous_order_id:
        return previous_order_id, SLOT_INHERITED
    return None, None
