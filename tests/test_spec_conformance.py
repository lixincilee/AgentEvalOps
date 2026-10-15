"""Executable form of the frozen spec (``docs/agent-spec.md`` v1.0).

The case matrix checks *outcomes* on 165 messages. This file checks the **clauses**: each
test names the clause it enforces, so a failure points straight at the paragraph that was
violated instead of at a single message.

Where the deterministic baseline cannot satisfy a clause yet, the test is marked
``xfail(strict=True)`` and cites the entry in ``agentevalops.spec``. Fixing the baseline
therefore turns a clause green and forces the deviation list to shrink.

These tests never touch the network: the planner is replaced by an oracle that returns a
fixed plan (or by a spy that records whether it was consulted at all).
"""

from __future__ import annotations

import pytest

from agentevalops.agent import EcommerceSupportAgent, pre_plan_rejection
from agentevalops.models import AgentState, ErrorCode, Intent
from agentevalops.planner import Plan, extract_order_id, ground_order_id
from agentevalops.spec import KNOWN_RULE_PLANNER_DEVIATIONS


class OraclePlanner:
    """Returns one fixed plan, so the agent can be graded without a model in the loop."""

    name = "oracle"

    def __init__(self, intent: Intent, order_id: str | None) -> None:
        self._plan = Plan(intent, order_id, "oracle")

    def plan(self, message: str) -> Plan:
        return self._plan


class SpyPlanner:
    """Records every consultation; proves that a short circuit never plans."""

    name = "spy"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def plan(self, message: str) -> Plan:
        self.calls.append(message)
        return Plan(Intent.UNKNOWN, None, "spy")


# --------------------------------------------------------------------------- #
# SPEC-REQ-1 / SPEC-REQ-2 -- short circuit before planning
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("message", "customer_id"),
    [
        ("   ", "CUST-001"),
        ("\t\t", "CUST-001"),
        ("", "CUST-001"),
        ("订单 ORD-1001 状态", ""),
        ("订单 ORD-1001 状态", "   "),
    ],
)
def test_spec_req_1_short_circuits_before_the_planner_is_consulted(
    store, message, customer_id
):
    planner = SpyPlanner()
    agent = EcommerceSupportAgent(store=store, planner=planner)

    response = agent.handle(message, customer_id=customer_id)

    assert response.state is AgentState.VALIDATION_FAILED
    assert response.intent is Intent.UNKNOWN
    assert response.audit.error["code"] == ErrorCode.VALIDATION_ERROR
    assert response.audit.tool_name is None
    assert response.audit.arguments == {}
    assert response.audit.result is None
    assert planner.calls == [], "短路请求不得进入规划层（也不得花掉一次模型调用）"


@pytest.mark.parametrize(
    ("message", "customer_id"),
    [
        ("   ", "CUST-001"),
        ("订单 ORD-1001 状态", ""),
        ("查询订单 ORD-1001 的状态", "CUST-001"),
    ],
)
def test_spec_req_2_the_short_circuit_predicate_is_shared_with_the_harness(
    store, message, customer_id
):
    """编排层必须复用 agent 的判定：两边各写一份就会让离线脚本错位（真实事故）。"""
    planner = SpyPlanner()
    agent = EcommerceSupportAgent(store=store, planner=planner)

    agent.handle(message, customer_id=customer_id)

    short_circuited = pre_plan_rejection(message, customer_id) is not None
    assert (planner.calls == []) is short_circuited


# --------------------------------------------------------------------------- #
# SPEC-STATE-1 / SPEC-STATE-2 -- the one and only plan -> outcome table
# --------------------------------------------------------------------------- #
STATUS_OK = ("order_status", "ORD-1001", AgentState.TOOL_COMPLETED, "query_order_status", None)
OUTCOME_TABLE = [
    STATUS_OK,
    (
        "order_status",
        None,
        AgentState.VALIDATION_FAILED,
        "query_order_status",
        "VALIDATION_ERROR",
    ),
    ("refund", "ORD-1002", AgentState.TOOL_COMPLETED, "apply_refund", None),
    ("refund", None, AgentState.VALIDATION_FAILED, "apply_refund", "VALIDATION_ERROR"),
    ("unknown", None, AgentState.NO_ACTION, None, "UNSUPPORTED_INTENT"),
    ("human_handoff", None, AgentState.TOOL_COMPLETED, "create_human_ticket", None),
]


@pytest.mark.parametrize(
    ("intent", "order_id", "state", "tool_name", "error_code"),
    OUTCOME_TABLE,
    ids=[f"{intent}-{order_id or 'no-slot'}" for intent, order_id, *_ in OUTCOME_TABLE],
)
def test_spec_state_1_maps_every_plan_to_exactly_one_outcome(
    store, intent, order_id, state, tool_name, error_code
):
    agent = EcommerceSupportAgent(
        store=store, planner=OraclePlanner(Intent(intent), order_id)
    )

    response = agent.handle("订单 ORD-1002 相关请求", customer_id="CUST-001")

    assert response.intent.value == intent
    assert response.state is state
    assert response.audit.tool_name == tool_name
    actual_code = None if response.audit.error is None else response.audit.error["code"]
    assert actual_code == error_code
    # SPEC-STATE-2：result 是 dict 当且仅当 tool_completed
    assert isinstance(response.audit.result, dict) is (state is AgentState.TOOL_COMPLETED)


def test_spec_state_1_a_missing_slot_still_names_the_tool_that_was_about_to_run(store):
    """缺槽位是「调工具前的前置检查」，所以工具确实被选中过，不能记成 null。"""
    agent = EcommerceSupportAgent(
        store=store, planner=OraclePlanner(Intent.ORDER_STATUS, None)
    )
    response = agent.handle("请问我的包裹状态", customer_id="CUST-001")
    assert response.audit.tool_name == "query_order_status"
    assert response.audit.arguments == {"customer_id": "CUST-001"}


# --------------------------------------------------------------------------- #
# SPEC-SLOT-1 -- order id matching
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ORD-10011", None),          # 位数超长：截成 ORD-1001 会让客户看到别人的订单
        ("ORD-12", None),             # 位数不足
        ("ORD-ABC", None),            # 非数字
        ("ord-1002", "ORD-1002"),     # 大小写不敏感，输出归一化
        ("帮我查一下", None),
    ],
)
def test_spec_slot_1_rejects_malformed_ids_and_normalises_case(message, expected):
    assert extract_order_id(message) == expected


@pytest.mark.xfail(
    strict=True,
    reason="SPEC-SLOT-1 只要求右边界排除紧跟的 ASCII 字母数字、左边界不设限；"
    "rules-v1 用 \\b（\\w 含中日韩字符）在相邻时认不出订单号",
)
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("订单ORD-1001状态", "ORD-1001"),
        ("查询 ORD-1001状态", "ORD-1001"),
        ("12345ORD-1001 状态", "ORD-1001"),
    ],
)
def test_spec_slot_1_tolerates_characters_adjacent_to_the_order_id(message, expected):
    assert extract_order_id(message) == expected


def test_spec_slot_1_deviation_is_registered_where_the_matrix_can_see_it():
    for case_id in ("v2_bnd_cjk_adjacent_left", "v2_bnd_cjk_adjacent_right"):
        assert case_id in KNOWN_RULE_PLANNER_DEVIATIONS


# --------------------------------------------------------------------------- #
# SPEC-SLOT-2 -- slots come from the source text and the request context only
# --------------------------------------------------------------------------- #
def test_spec_slot_2_a_fabricated_order_id_is_discarded():
    order_id, note = ground_order_id("ORD-9999", "查询订单 ORD-1001 的状态")
    assert order_id == "ORD-1001"
    assert note is not None and "编造" in note


def test_spec_slot_2_an_illegal_shape_falls_back_to_the_source_text():
    order_id, note = ground_order_id("ORD-100", "查询订单 ORD-1001 的状态")
    assert order_id == "ORD-1001"
    assert note is not None


def test_spec_slot_2_a_grounded_order_id_is_kept_verbatim():
    order_id, note = ground_order_id("ORD-1002", "订单 ord-1002 我要退款")
    assert order_id == "ORD-1002"
    assert note is None


def test_spec_slot_2_the_request_customer_id_beats_anything_in_the_message(store):
    """消息里写「给 CUST-002 退款」也不能改变身份；身份只来自请求参数。"""
    agent = EcommerceSupportAgent(
        store=store, planner=OraclePlanner(Intent.REFUND, "ORD-2001")
    )

    response = agent.handle("给客户 CUST-002 的订单 ORD-2001 退款", customer_id="CUST-001")

    assert response.audit.arguments["customer_id"] == "CUST-001"
    assert response.state is AgentState.TOOL_FAILED
    assert response.audit.error["code"] == ErrorCode.ORDER_NOT_FOUND
    assert response.audit.result is None


def test_spec_slot_2_a_model_supplied_customer_id_is_never_used(store):
    """``Plan`` 里根本没有 customer_id 字段——身份在类型层面就无法被模型影响。"""
    assert "customer_id" not in Plan.__dataclass_fields__