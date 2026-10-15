"""Contract tests: the stable shapes and vocabularies every component must honour."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from agentevalops.models import AgentState, AuditRecord, ErrorCode, Intent, ToolResult

TOOL_FIXTURES = ("status_tool", "refund_tool", "ticket_tool")
EXPECTED_TOOL_NAMES = {
    "status_tool": "query_order_status",
    "refund_tool": "apply_refund",
    "ticket_tool": "create_human_ticket",
}


def test_tool_result_ok_carries_payload_and_no_error():
    result = ToolResult.ok(order_id="ORD-1001")
    assert result.success is True
    assert result.data == {"order_id": "ORD-1001"}
    assert result.error_code is None
    assert result.error_message is None


def test_tool_result_fail_carries_error_and_no_payload():
    result = ToolResult.fail(ErrorCode.ORDER_NOT_FOUND, "order was not found")
    assert result.success is False
    assert result.data == {}
    assert result.error_code == "ORDER_NOT_FOUND"
    assert result.error_message == "order was not found"


def test_tool_result_to_dict_has_a_frozen_schema():
    payload = ToolResult.fail(ErrorCode.INVALID_ARGUMENT, "bad input").to_dict()
    assert set(payload) == {"success", "data", "error_code", "error_message"}
    assert json.loads(json.dumps(payload))["error_code"] == "INVALID_ARGUMENT"


def test_tool_result_is_immutable():
    result = ToolResult.ok(status="shipped")
    with pytest.raises(FrozenInstanceError):
        result.success = False  # type: ignore[misc]


@pytest.mark.parametrize("fixture_name", TOOL_FIXTURES)
def test_every_tool_exposes_a_callable_contract(fixture_name, request):
    tool = request.getfixturevalue(fixture_name)
    assert tool.name == EXPECTED_TOOL_NAMES[fixture_name]
    assert callable(tool.execute)


def test_every_refusal_uses_a_documented_error_code(status_tool, refund_tool, ticket_tool):
    observed = {
        status_tool.execute(order_id="ORD-9999", customer_id="CUST-001").error_code,
        status_tool.execute(order_id="", customer_id="CUST-001").error_code,
        refund_tool.execute(order_id="ORD-1001", customer_id="CUST-001", reason="r").error_code,
        refund_tool.execute(order_id="ORD-9999", customer_id="CUST-001", reason="r").error_code,
        refund_tool.execute(order_id="ORD-1002", customer_id="CUST-001", reason="").error_code,
        ticket_tool.execute(customer_id="CUST-001", summary="s", order_id="ORD-2001").error_code,
        ticket_tool.execute(customer_id="", summary="s").error_code,
    }
    assert observed <= {code.value for code in ErrorCode}


def test_audit_record_exposes_every_required_field():
    record = AuditRecord(
        intent=Intent.ORDER_STATUS,
        state=AgentState.TOOL_COMPLETED,
        tool_name="query_order_status",
        arguments={"order_id": "ORD-1001"},
        result={"status": "shipped"},
        error=None,
    )
    assert set(record.to_dict()) == {
        "intent",
        "state",
        "tool_name",
        "arguments",
        "result",
        "error",
    }


def test_intent_vocabulary_is_stable():
    # 词汇表的每一次增长都必须来自一次规格升版（v1.1 加入 policy_qa）。
    # 这个测试故意写得「笨」：新增取值会让它变红，逼人回来确认那是一次有意的规格变更。
    assert {intent.value for intent in Intent} == {
        "order_status",
        "refund",
        "human_handoff",
        "policy_qa",
        "unknown",
    }


def test_agent_state_vocabulary_is_stable():
    assert {state.value for state in AgentState} == {
        "received",
        "validation_failed",
        "tool_completed",
        "tool_failed",
        "no_action",
    }


def test_error_code_vocabulary_is_stable():
    assert {code.value for code in ErrorCode} == {
        "INVALID_ARGUMENT",
        "ORDER_NOT_FOUND",
        "REFUND_NOT_ALLOWED",
        "VALIDATION_ERROR",
        "UNSUPPORTED_INTENT",
        "NO_KNOWLEDGE",
    }


def test_agent_response_is_json_serializable(agent):
    response = agent.handle("请查询订单 ORD-1001 的物流状态", customer_id="CUST-001")
    payload = json.loads(json.dumps(response.to_dict(), ensure_ascii=False))
    assert payload["intent"] == "order_status"
    assert payload["state"] == "tool_completed"
    assert payload["audit"]["tool_name"] == "query_order_status"
