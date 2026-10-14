"""The planning layer: v1 rules, v2 model-driven, and the guards between them."""

from __future__ import annotations

import json

import pytest
from conftest import load_support_cases

from agentevalops.agent import EcommerceSupportAgent
from agentevalops.llm import LLMUnavailableError, ScriptedLLMClient
from agentevalops.models import AgentState, Intent
from agentevalops.planner import (
    SOURCE_FALLBACK,
    SOURCE_LLM,
    SOURCE_RULES,
    LLMPlanner,
    Plan,
    RulePlanner,
    coerce_intent,
    ground_order_id,
    parse_plan_json,
)

CASES = load_support_cases()


class BoomClient:
    """A client whose only job is to fail, for the fault-injection path."""

    model_id = "boom"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def complete(self, messages, *, temperature=None, max_tokens=None):
        raise self.exc


def llm_planner(reply: str, **kwargs) -> LLMPlanner:
    return LLMPlanner(ScriptedLLMClient([reply]), **kwargs)


def reply_for(intent: str, order_id: str | None) -> str:
    return json.dumps({"intent": intent, "order_id": order_id})


# --- v1: the deterministic baseline ------------------------------------------------


@pytest.mark.parametrize(
    ("message", "intent", "order_id"),
    [
        ("请查询订单 ORD-1001 的物流状态", Intent.ORDER_STATUS, "ORD-1001"),
        ("我要退款，订单 ord-1002", Intent.REFUND, "ORD-1002"),
        ("what is the shipping status of order ORD-1003", Intent.ORDER_STATUS, "ORD-1003"),
        ("我要找人工客服", Intent.HUMAN_HANDOFF, None),
        ("今天天气不错", Intent.UNKNOWN, None),
    ],
)
def test_rule_planner_matches_the_v1_baseline(message, intent, order_id):
    plan = RulePlanner().plan(message)
    assert plan == Plan(intent, order_id, SOURCE_RULES)
    assert plan.note is None


@pytest.mark.parametrize(
    "message",
    [
        "help me track order ORD-1003",
        "ORD-1003 查一下",
        "where is my parcel ORD-1003",
    ],
)
def test_v1_has_documented_false_negatives_that_v2_is_meant_to_fix(message):
    """v1 needs BOTH a status word and an order word; these are the gaps to measure."""
    plan = RulePlanner().plan(message)
    assert plan.intent is Intent.UNKNOWN
    assert plan.order_id == "ORD-1003"


def test_rule_planner_is_named_and_satisfies_the_planner_protocol():
    assert RulePlanner().name == "rules-v1"


# --- parsing the model reply -------------------------------------------------------


def test_parse_plan_json_accepts_a_bare_object():
    assert parse_plan_json('{"intent": "refund"}') == {"intent": "refund"}


def test_parse_plan_json_unwraps_a_fenced_block():
    assert parse_plan_json('```json\n{"intent": "refund"}\n```') == {"intent": "refund"}


def test_parse_plan_json_ignores_surrounding_prose():
    assert parse_plan_json('好的：{"intent": "refund"} 以上。') == {"intent": "refund"}


@pytest.mark.parametrize(
    "text", ["", "   ", "不是 JSON", "[1, 2]", "```\nnope\n```", "{broken"]
)
def test_parse_plan_json_returns_none_for_anything_else(text):
    assert parse_plan_json(text) is None


def test_coerce_intent_accepts_aliases_case_insensitively():
    assert coerce_intent(" REFUND ") is Intent.REFUND
    assert coerce_intent("Order_Status") is Intent.ORDER_STATUS
    assert coerce_intent("escalate") is Intent.HUMAN_HANDOFF


@pytest.mark.parametrize("value", ["转账", "", None, 7, ["refund"]])
def test_coerce_intent_rejects_anything_unmapped(value):
    assert coerce_intent(value) is None


# --- the hallucination guard -------------------------------------------------------


def test_ground_order_id_keeps_an_id_the_user_actually_wrote():
    assert ground_order_id("ord-1001", "订单 ORD-1001 到哪了") == ("ORD-1001", None)


def test_ground_order_id_refuses_an_invented_order():
    order_id, note = ground_order_id("ORD-9999", "订单 ORD-1001 到哪了")
    assert order_id == "ORD-1001"
    assert note is not None and "编造" in note


def test_ground_order_id_refuses_a_malformed_order():
    order_id, note = ground_order_id("1001", "订单 ORD-1001 到哪了")
    assert order_id == "ORD-1001"
    assert note is not None and "格式非法" in note


def test_ground_order_id_backfills_a_missing_order():
    order_id, note = ground_order_id(None, "订单 ORD-1001 到哪了")
    assert order_id == "ORD-1001"
    assert note is not None and "补全" in note


def test_ground_order_id_stays_none_when_the_message_has_no_order():
    assert ground_order_id(None, "我要退款") == (None, None)
    assert ground_order_id("ORD-9999", "我要退款") == (None, "模型编造了订单号 ORD-9999，已按原文抽取")


# --- v2: the model-driven planner --------------------------------------------------


def test_llm_planner_uses_the_model_answer():
    planner = llm_planner(reply_for("refund", "ORD-1002"))
    plan = planner.plan("订单 ORD-1002 我要退款")
    assert plan == Plan(Intent.REFUND, "ORD-1002", SOURCE_LLM)
    assert planner.last_response is not None


def test_llm_planner_is_versioned_by_its_prompt():
    assert llm_planner(reply_for("unknown", None)).name == "llm-v2"


def test_llm_planner_reads_a_fenced_answer():
    planner = llm_planner('```json\n{"intent": "order_status", "order_id": "ORD-1001"}\n```')
    assert planner.plan("ORD-1001 状态").intent is Intent.ORDER_STATUS


@pytest.mark.parametrize(
    ("reply", "guard"),
    [
        ("抱歉，我不确定。", "不是合法 JSON"),
        ('{"intent": "转账", "order_id": null}', "intent 非法"),
        ('{"order_id": null}', "intent 非法"),
    ],
)
def test_llm_planner_falls_back_when_the_answer_is_unusable(reply, guard):
    plan = llm_planner(reply).plan("请查询订单 ORD-1001 的物流状态")
    assert plan.source == SOURCE_FALLBACK
    assert plan.intent is Intent.ORDER_STATUS  # v1 rescued the decision
    assert plan.order_id == "ORD-1001"
    assert guard in (plan.note or "")


def test_llm_planner_falls_back_when_the_model_is_unavailable():
    planner = LLMPlanner(BoomClient(LLMUnavailableError("timeout")))
    plan = planner.plan("订单 ORD-1001 到哪了")
    assert plan.source == SOURCE_FALLBACK
    assert plan.intent is Intent.ORDER_STATUS
    assert "LLMUnavailableError" in (plan.note or "")


def test_llm_planner_falls_back_when_the_script_runs_out():
    plan = LLMPlanner(ScriptedLLMClient()).plan("订单 ORD-1001 到哪了")
    assert plan.source == SOURCE_FALLBACK
    assert plan.order_id == "ORD-1001"


def test_llm_planner_keeps_the_guard_note_on_a_usable_answer():
    planner = llm_planner(reply_for("order_status", "ORD-9999"))
    plan = planner.plan("订单 ORD-1001 到哪了")
    assert plan.source == SOURCE_LLM
    assert plan.order_id == "ORD-1001"
    assert plan.note is not None


# --- end to end: swapping the planner must not move the contract -------------------


def test_v1_and_v2_produce_an_identical_audit_trail(agent):
    message = "我要退款，订单 ORD-1002，包装破损"
    v2 = EcommerceSupportAgent(
        agent.store, planner=llm_planner(reply_for("refund", "ORD-1002"))
    )
    assert v2.handle(message, customer_id="CUST-001").to_dict() == agent.handle(
        message, customer_id="CUST-001"
    ).to_dict()


def test_agent_records_the_plan_it_acted_on(agent):
    agent.handle("订单 ORD-1001 到哪了", customer_id="CUST-001")
    assert agent.last_plan == Plan(Intent.ORDER_STATUS, "ORD-1001", SOURCE_RULES)


def test_a_hallucinated_order_id_never_becomes_a_tool_argument(agent):
    """The model invents ORD-9999; the guard drops it, so validation still blocks."""
    v2 = EcommerceSupportAgent(
        agent.store, planner=llm_planner(reply_for("refund", "ORD-9999"))
    )
    response = v2.handle("我要退款", customer_id="CUST-001")
    assert response.state is AgentState.VALIDATION_FAILED
    assert response.audit.arguments == {"customer_id": "CUST-001"}


@pytest.mark.parametrize("case", CASES, ids=[c["case_id"] for c in CASES])
def test_v2_replays_the_v1_scenario_set(case, agent):
    """Data-driven regression net: scripted model answers must reproduce v1 results."""
    order_id = case["expected_arguments"].get("order_id")
    v2 = EcommerceSupportAgent(
        agent.store, planner=llm_planner(reply_for(case["expected_intent"], order_id))
    )
    response = v2.handle(case["message"], customer_id=case["customer_id"])
    assert response.intent.value == case["expected_intent"], case["notes"]
    assert response.state.value == case["expected_state"], case["notes"]
    assert response.audit.tool_name == case["expected_tool"], case["notes"]
