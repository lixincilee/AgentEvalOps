"""故障注入脚手架本身：契约、边界，以及「不许把 bug 伪装成故障」那一条（SPEC-FAULT-1..6）。

这一层测的不是 Agent 的功能，而是**实验装置**：注入错了、注错地方了、或者根本没注入，
后面的故障报告就会变成一份「一切正常」的假证据。所以每条守卫都对着一种假证据来写。
"""

from __future__ import annotations

import pytest

from agentevalops.agent import EcommerceSupportAgent
from agentevalops.faults import (
    SEARCH_TOOL,
    FaultSpec,
    FaultSpecError,
    FaultyTool,
    build_faulted_agent,
)
from agentevalops.models import AgentState, ErrorCode, Intent
from agentevalops.order_store import OrderStore
from agentevalops.pipeline import V3Agent
from agentevalops.tools import OrderStatusTool, ToolTimeout, ToolUnavailable

POLICY_QUESTION = "退款政策是怎么规定的"
REFUND_MESSAGE = "订单 ORD-1004 我要退款"
STATUS_MESSAGE = "我的订单 ORD-1001 到哪了"


def test_an_unknown_target_is_rejected_instead_of_silently_not_injecting():
    """声明写错必须炸。悄悄不注入 = 用例「通过」而故障从没发生过。"""
    with pytest.raises(FaultSpecError, match="fault.target"):
        FaultSpec.parse({"target": "database", "mode": "timeout"})


@pytest.mark.parametrize(
    "raw",
    [
        {"target": "tool", "mode": "empty", "tool_name": "apply_refund"},
        {"target": "tool", "mode": "timeout"},
        {"target": "tool", "mode": "timeout", "tool_name": "send_email"},
        {"target": "retrieval", "mode": "empty", "tool_name": "query_order_status"},
        {"target": "retrieval", "mode": "rate_limit"},
        {"target": "model", "mode": "rate_limit", "tool_name": "query_order_status"},
        {"target": "model", "mode": "timeout"},
    ],
)
def test_an_unusable_declaration_is_rejected(raw):
    with pytest.raises(FaultSpecError):
        FaultSpec.parse(raw)


@pytest.mark.parametrize(
    "mode,expected",
    [("timeout", ToolTimeout), ("unavailable", ToolUnavailable)],
)
def test_the_injected_exception_type_follows_the_mode(mode, expected):
    tool = FaultyTool(
        OrderStatusTool(OrderStore()),
        FaultSpec.parse({"target": "tool", "mode": mode, "tool_name": "query_order_status"}),
    )
    with pytest.raises(expected):
        tool.execute(order_id="ORD-1001", customer_id="CUST-001")


def test_a_faulty_tool_keeps_the_inner_name():
    """故障不许改名：审计里的 tool_name 是「哪个工具坏了」的唯一来源。"""
    tool = FaultyTool(
        OrderStatusTool(OrderStore()),
        FaultSpec.parse({"target": "tool", "mode": "timeout", "tool_name": "query_order_status"}),
    )
    assert tool.name == "query_order_status"


def test_a_tool_timeout_becomes_tool_failed_and_keeps_the_evidence():
    agent = build_faulted_agent(
        {"target": "tool", "mode": "timeout", "tool_name": "query_order_status"}
    )
    response = agent.handle(STATUS_MESSAGE, customer_id="CUST-001")

    assert response.state is AgentState.TOOL_FAILED
    assert response.intent is Intent.ORDER_STATUS
    assert response.audit.tool_name == "query_order_status"
    assert response.audit.error["code"] == ErrorCode.TOOL_TIMEOUT
    assert response.audit.result is None


def test_a_timed_out_write_tool_is_never_retried():
    """SPEC-FAULT-2：超时后重试退款，等于给「重复退款」留一条路。"""
    seen: list[dict] = []

    class CountingRefund:
        name = "apply_refund"

        def execute(self, **arguments):
            seen.append(arguments)
            raise ToolTimeout("apply_refund 超时", tool_name=self.name)

    agent = EcommerceSupportAgent(OrderStore())
    agent.refund_tool = CountingRefund()
    response = agent.handle(REFUND_MESSAGE, customer_id="CUST-001")

    assert response.state is AgentState.TOOL_FAILED
    assert response.audit.error["code"] == ErrorCode.TOOL_TIMEOUT
    assert len(seen) == 1


def test_a_bug_is_never_disguised_as_a_tool_fault():
    """SPEC-FAULT-6：只有 ToolError 才被转换。别的异常必须继续往上抛。"""

    class Exploding:
        name = "query_order_status"

        def execute(self, **arguments):
            raise KeyError("这是代码 bug，不是依赖故障")

    agent = EcommerceSupportAgent(OrderStore())
    agent.order_status_tool = Exploding()
    with pytest.raises(KeyError):
        agent.handle(STATUS_MESSAGE, customer_id="CUST-001")


def test_a_retrieval_fault_is_not_a_knowledge_gap():
    """链路坏了要给 TOOL_TIMEOUT，不能退化成 NO_KNOWLEDGE——两者的处置完全不同。"""
    agent = build_faulted_agent({"target": "retrieval", "mode": "timeout"})
    response = agent.handle(POLICY_QUESTION, customer_id="CUST-001")

    assert response.intent is Intent.POLICY_QA
    assert response.state is AgentState.TOOL_FAILED
    assert response.audit.tool_name == SEARCH_TOOL
    assert response.audit.error["code"] == ErrorCode.TOOL_TIMEOUT


def test_a_retrieval_fault_cannot_change_a_business_answer():
    """注入必须局限于被注入的那一环：业务请求本来就不走检索。"""
    agent = build_faulted_agent({"target": "retrieval", "mode": "unavailable"})
    response = agent.handle(REFUND_MESSAGE, customer_id="CUST-001")

    assert response.state is AgentState.TOOL_COMPLETED
    assert response.audit.tool_name == "apply_refund"
    assert "retrieve" not in agent.last_nodes


def test_an_injected_empty_hit_list_refuses_instead_of_inventing_a_document():
    question = POLICY_QUESTION
    grounded = V3Agent(OrderStore()).handle(question, customer_id="CUST-001")
    assert grounded.state is AgentState.TOOL_COMPLETED

    injected = build_faulted_agent({"target": "retrieval", "mode": "empty"})
    response = injected.handle(question, customer_id="CUST-001")

    assert response.intent is Intent.POLICY_QA
    assert response.state is AgentState.NO_ACTION
    assert response.audit.tool_name is None
    assert response.audit.error["code"] == ErrorCode.NO_KNOWLEDGE


def test_injected_empty_retrieval_is_indistinguishable_from_a_genuine_gap():
    """SPEC-FAULT-5：两条路必须逐字段一样，否则这套用例测的是「注入生效没有」。"""
    question = "宠物寄存政策是什么"  # 语料里真的没有对应文档
    genuine = V3Agent(OrderStore()).handle(question, customer_id="CUST-001")
    injected = build_faulted_agent({"target": "retrieval", "mode": "empty"}).handle(
        question, customer_id="CUST-001"
    )
    assert genuine.to_dict() == injected.to_dict()


def test_a_rate_limited_model_degrades_instead_of_failing_the_request():
    agent = build_faulted_agent({"target": "model", "mode": "rate_limit"})
    response = agent.handle(STATUS_MESSAGE, customer_id="CUST-001")

    assert response.state is AgentState.TOOL_COMPLETED
    assert agent.last_plan.source == "llm-fallback"
    assert agent.fault_client.rate_limited == 1


def test_a_rate_limited_model_still_routes_a_policy_question_to_retrieval():
    """降级路径必须能表达 policy_qa —— v0.5.1 修掉的正是这一类（见证据文档 §10.3）。"""
    agent = build_faulted_agent({"target": "model", "mode": "rate_limit"})
    response = agent.handle(POLICY_QUESTION, customer_id="CUST-001")

    assert response.intent is Intent.POLICY_QA
    assert response.state is AgentState.TOOL_COMPLETED
    assert response.audit.tool_name == SEARCH_TOOL
    assert agent.last_retrieval.top_doc_id is not None


def test_faults_do_not_leak_between_agents():
    build_faulted_agent(
        {"target": "tool", "mode": "timeout", "tool_name": "query_order_status"}
    )
    clean = V3Agent(OrderStore()).handle(STATUS_MESSAGE, customer_id="CUST-001")
    assert clean.state is AgentState.TOOL_COMPLETED
