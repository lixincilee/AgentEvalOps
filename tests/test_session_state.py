"""Multi-turn state: slot inheritance, source labels, and session isolation.

SPEC-MT-2 / 3 / 4 / 6. Every assertion here exists to protect one property: an inherited
slot must never be able to pass itself off as something the user actually typed, and one
customer's state must never be reachable from another customer's session.
"""

from __future__ import annotations

from agentevalops.models import SLOT_INHERITED, SLOT_VERBATIM, Intent
from agentevalops.session import (
    SLOT_INTENTS,
    SessionState,
    SessionStore,
    Turn,
    resolve_slot,
)


# --- resolve_slot ------------------------------------------------------------


def test_a_message_that_prints_an_order_id_yields_a_verbatim_slot():
    assert resolve_slot("帮我查订单 ORD-1001 的状态") == ("ORD-1001", SLOT_VERBATIM)


def test_a_message_without_an_order_id_inherits_the_previous_one():
    assert resolve_slot("这个订单我要退款", previous_order_id="ORD-1004") == (
        "ORD-1004",
        SLOT_INHERITED,
    )


def test_a_message_without_an_order_id_and_without_history_has_no_slot():
    assert resolve_slot("这个订单我要退款") == (None, None)


def test_the_message_always_beats_the_previous_turn():
    """SPEC-MT-2：继承只在消息本身没有订单号时生效。"""
    assert resolve_slot("订单 ORD-1002 状态", previous_order_id="ORD-1004") == (
        "ORD-1002",
        SLOT_VERBATIM,
    )


def test_an_inherited_slot_is_never_labelled_verbatim():
    order_id, source = resolve_slot("那笔要退", previous_order_id="ORD-1004")
    assert order_id == "ORD-1004"
    assert source == SLOT_INHERITED
    assert source != SLOT_VERBATIM


def test_a_pronoun_resolves_to_the_previous_order_id():
    """SPEC-MT-4：有上一轮订单号时，指代词指向它。"""
    for message in ("这个要退款", "那笔有问题", "它怎么还没到", "这一单我要投诉"):
        assert resolve_slot(message, previous_order_id="ORD-1004")[0] == "ORD-1004"


def test_a_pronoun_without_history_is_a_missing_slot_not_a_guess():
    """SPEC-MT-4：没有上一轮订单号时视为缺槽位，不得猜测。"""
    assert resolve_slot("这个要退款") == (None, None)


def test_a_planner_provided_slot_is_accepted_as_verbatim():
    assert resolve_slot("查一下状态", planned="ORD-1001") == ("ORD-1001", SLOT_VERBATIM)


def test_the_slot_intents_are_the_three_business_actions():
    assert SLOT_INTENTS == {Intent.ORDER_STATUS, Intent.REFUND, Intent.HUMAN_HANDOFF}
    assert Intent.POLICY_QA not in SLOT_INTENTS
    assert Intent.UNKNOWN not in SLOT_INTENTS


# --- SessionState ------------------------------------------------------------


def test_remember_appends_the_turn():
    session = SessionState(session_id="MT-001", customer_id="CUST-001")
    session.remember("查订单 ORD-1001", Intent.ORDER_STATUS, "ORD-1001", SLOT_VERBATIM)
    assert len(session.turns) == 1
    assert session.turns[0].order_id == "ORD-1001"
    assert session.turns[0].slot_source == SLOT_VERBATIM


def test_remember_updates_the_last_order_id_for_a_slot_intent():
    session = SessionState(session_id="MT-001", customer_id="CUST-001")
    session.remember("查订单 ORD-1001", Intent.ORDER_STATUS, "ORD-1001", SLOT_VERBATIM)
    assert session.last_order_id == "ORD-1001"
    assert session.last_intent is Intent.ORDER_STATUS


def test_a_policy_question_does_not_overwrite_the_slot():
    """policy_qa 不消费订单号，所以它也不能污染会话里的槽位。"""
    session = SessionState(session_id="MT-001", customer_id="CUST-001")
    session.remember("查订单 ORD-1001", Intent.ORDER_STATUS, "ORD-1001", SLOT_VERBATIM)
    session.remember("退款政策是怎么规定的", Intent.POLICY_QA, None, None)
    assert session.last_order_id == "ORD-1001"
    assert session.last_intent is Intent.POLICY_QA


def test_a_turn_that_resolved_no_slot_leaves_the_previous_one_intact():
    session = SessionState(session_id="MT-001", customer_id="CUST-001")
    session.remember("查订单 ORD-1001", Intent.ORDER_STATUS, "ORD-1001", SLOT_VERBATIM)
    session.remember("这个要退款", Intent.REFUND, None, None)
    assert session.last_order_id == "ORD-1001"


def test_hint_is_absent_without_a_slot():
    assert SessionState(session_id="MT-001", customer_id="CUST-001").hint() is None


def test_hint_carries_the_last_order_id():
    session = SessionState(session_id="MT-001", customer_id="CUST-001")
    session.remember("查订单 ORD-1001", Intent.ORDER_STATUS, "ORD-1001", SLOT_VERBATIM)
    assert "ORD-1001" in session.hint()


def test_hint_never_names_an_intent():
    """SPEC-MT-3：意图不继承。提示里出现意图名，等于把上一轮的判断塞给这一轮。"""
    session = SessionState(session_id="MT-001", customer_id="CUST-001")
    session.remember("查订单 ORD-1001", Intent.ORDER_STATUS, "ORD-1001", SLOT_VERBATIM)
    hint = session.hint().casefold()
    for name in ("order_status", "refund", "human_handoff", "policy_qa", "意图"):
        assert name not in hint


def test_turn_and_session_serialise_to_plain_data():
    turn = Turn("查订单 ORD-1001", Intent.ORDER_STATUS, "ORD-1001", SLOT_VERBATIM)
    assert turn.to_dict() == {
        "message": "查订单 ORD-1001",
        "intent": "order_status",
        "order_id": "ORD-1001",
        "slot_source": SLOT_VERBATIM,
    }
    session = SessionState(session_id="MT-001", customer_id="CUST-001")
    session.remember("查订单 ORD-1001", Intent.ORDER_STATUS, "ORD-1001", SLOT_VERBATIM)
    payload = session.to_dict()
    assert payload["session_id"] == "MT-001"
    assert payload["last_intent"] == "order_status"
    assert payload["turns"] == [turn.to_dict()]


# --- SessionStore（SPEC-MT-6） -----------------------------------------------


def test_state_for_creates_once_and_then_returns_the_same_object():
    store = SessionStore()
    first = store.state_for("MT-001", "CUST-001")
    assert store.state_for("MT-001", "CUST-001") is first
    assert len(store) == 1


def test_get_returns_nothing_before_the_session_exists():
    assert SessionStore().get("MT-001", "CUST-001") is None


def test_the_same_session_id_under_another_customer_is_a_different_session():
    """这是这套系统最典型的一类越权来源：只按 session_id 存状态。"""
    store = SessionStore()
    mine = store.state_for("MT-001", "CUST-001")
    mine.remember("查订单 ORD-1004", Intent.ORDER_STATUS, "ORD-1004", SLOT_VERBATIM)
    theirs = store.state_for("MT-001", "CUST-002")
    assert theirs is not mine
    assert theirs.last_order_id is None
    assert len(store) == 2


def test_the_same_customer_in_another_session_starts_clean():
    store = SessionStore()
    first = store.state_for("MT-001", "CUST-001")
    first.remember("查订单 ORD-1004", Intent.ORDER_STATUS, "ORD-1004", SLOT_VERBATIM)
    assert store.state_for("MT-002", "CUST-001").last_order_id is None


def test_clear_drops_every_session():
    store = SessionStore()
    store.state_for("MT-001", "CUST-001")
    store.clear()
    assert len(store) == 0
    assert store.get("MT-001", "CUST-001") is None
