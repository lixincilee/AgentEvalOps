"""End-to-end tests for the deterministic support agent."""

from __future__ import annotations

import json

import pytest

from agentevalops.agent import EcommerceSupportAgent, pre_plan_rejection
from agentevalops.models import AgentState, ErrorCode, Intent


class TestOrderStatusFlow:
    def test_answers_a_status_question_with_a_complete_audit(self, agent):
        response = agent.handle("请查询订单 ORD-1001 的物流状态", customer_id="CUST-001")

        assert response.intent is Intent.ORDER_STATUS
        assert response.state is AgentState.TOOL_COMPLETED
        assert response.audit.tool_name == "query_order_status"
        assert response.audit.arguments == {
            "order_id": "ORD-1001",
            "customer_id": "CUST-001",
        }
        assert response.audit.result == {
            "order_id": "ORD-1001",
            "status": "shipped",
            "tracking_number": "SF1234567890",
        }
        assert response.audit.error is None

    def test_normalises_a_lowercase_order_id(self, agent):
        response = agent.handle("查询订单 ord-1002 的状态", customer_id="CUST-001")
        assert response.audit.arguments["order_id"] == "ORD-1002"
        assert response.audit.result["status"] == "delivered"

    def test_missing_order_id_is_caught_before_the_tool_runs(self, agent):
        response = agent.handle("帮我查询订单状态", customer_id="CUST-001")
        assert response.state is AgentState.VALIDATION_FAILED
        assert response.audit.tool_name == "query_order_status"
        assert response.audit.arguments == {"customer_id": "CUST-001"}
        assert response.audit.result is None
        assert response.audit.error["code"] == ErrorCode.VALIDATION_ERROR

    def test_unknown_order_records_a_tool_failure(self, agent):
        response = agent.handle("查询订单 ORD-9999 的状态", customer_id="CUST-001")
        assert response.state is AgentState.TOOL_FAILED
        assert response.audit.error["code"] == ErrorCode.ORDER_NOT_FOUND
        assert response.audit.result is None
        assert response.message == response.audit.error["message"]


class TestRefundFlow:
    def test_refund_on_a_delivered_order_succeeds(self, agent):
        message = "订单 ORD-1002 商品损坏，我要退款"
        response = agent.handle(message, customer_id="CUST-001")
        assert response.intent is Intent.REFUND
        assert response.state is AgentState.TOOL_COMPLETED
        assert response.audit.tool_name == "apply_refund"
        assert response.audit.arguments["reason"] == message
        assert response.audit.result["status"] == "submitted"
        assert response.audit.result["refund_id"].startswith("REF-")

    def test_refund_is_rejected_for_a_non_refundable_status(self, agent):
        response = agent.handle("退款订单 ORD-1001", customer_id="CUST-001")
        assert response.state is AgentState.TOOL_FAILED
        assert response.audit.error["code"] == ErrorCode.REFUND_NOT_ALLOWED

    def test_missing_order_id_is_caught_before_the_tool_runs(self, agent):
        response = agent.handle("我要退款", customer_id="CUST-001")
        assert response.state is AgentState.VALIDATION_FAILED
        assert response.audit.tool_name == "apply_refund"
        assert response.audit.error["code"] == ErrorCode.VALIDATION_ERROR

    def test_refund_does_not_mutate_the_order_store(self, agent, store):
        before = store.get("ORD-1002")
        agent.handle("订单 ORD-1002 我要退款", customer_id="CUST-001")
        assert store.get("ORD-1002") == before


class TestHumanHandoffFlow:
    def test_creates_a_ticket_linked_to_the_order(self, agent):
        response = agent.handle("订单 ORD-1001 有问题，转人工客服", customer_id="CUST-001")
        assert response.intent is Intent.HUMAN_HANDOFF
        assert response.state is AgentState.TOOL_COMPLETED
        assert response.audit.tool_name == "create_human_ticket"
        assert response.audit.result["status"] == "open"
        assert response.audit.result["order_id"] == "ORD-1001"

    def test_creates_a_ticket_without_an_order(self, agent):
        response = agent.handle("我要投诉，请转人工", customer_id="CUST-001")
        assert response.state is AgentState.TOOL_COMPLETED
        assert response.audit.result["order_id"] is None
        assert response.audit.arguments["order_id"] is None

    def test_rejects_a_ticket_for_another_customers_order(self, agent):
        response = agent.handle("订单 ORD-2001 异常，转人工", customer_id="CUST-001")
        assert response.state is AgentState.TOOL_FAILED
        assert response.audit.error["code"] == ErrorCode.ORDER_NOT_FOUND


class TestUnsupportedAndInvalidRequests:
    def test_unsupported_request_takes_no_action(self, agent):
        response = agent.handle("今天天气怎么样", customer_id="CUST-001")
        assert response.intent is Intent.UNKNOWN
        assert response.state is AgentState.NO_ACTION
        assert response.audit.tool_name is None
        assert response.audit.arguments == {}
        assert response.audit.result is None
        assert response.audit.error["code"] == ErrorCode.UNSUPPORTED_INTENT

    @pytest.mark.parametrize("message", ["", "   ", "\n\t", None, 123, ["订单 ORD-1001"]])
    def test_blank_or_non_string_message_is_rejected(self, agent, message):
        response = agent.handle(message, customer_id="CUST-001")
        assert response.state is AgentState.VALIDATION_FAILED
        assert response.audit.error["code"] == ErrorCode.VALIDATION_ERROR
        assert response.audit.tool_name is None

    @pytest.mark.parametrize("customer_id", ["", "   ", None, 42])
    def test_blank_or_non_string_customer_id_is_rejected(self, agent, customer_id):
        response = agent.handle("查询订单 ORD-1001 状态", customer_id=customer_id)
        assert response.state is AgentState.VALIDATION_FAILED
        assert response.audit.error["code"] == ErrorCode.VALIDATION_ERROR


class TestDeterminism:
    def test_repeated_calls_produce_identical_audits(self, agent):
        first = agent.handle("请查询订单 ORD-1001 的物流状态", customer_id="CUST-001")
        second = agent.handle("请查询订单 ORD-1001 的物流状态", customer_id="CUST-001")
        assert first.to_dict() == second.to_dict()

    def test_fresh_instances_produce_identical_audits(self, store):
        message, customer_id = "请查询订单 ORD-1001 的物流状态", "CUST-001"
        first = EcommerceSupportAgent(store=store).handle(message, customer_id=customer_id)
        second = EcommerceSupportAgent(store=store).handle(message, customer_id=customer_id)
        assert first.to_dict() == second.to_dict()

    def test_response_is_json_serializable(self, agent):
        response = agent.handle("请查询订单 ORD-1001 的物流状态", customer_id="CUST-001")
        payload = json.loads(json.dumps(response.to_dict(), ensure_ascii=False))
        assert payload["audit"]["result"]["status"] == "shipped"

    def test_repeating_a_refund_on_one_agent_is_idempotent(self, agent):
        first = agent.handle("订单 ORD-1002 我要退款", customer_id="CUST-001")
        second = agent.handle("订单 ORD-1002 我要退款", customer_id="CUST-001")
        assert first.audit.result["duplicate"] is False
        assert second.audit.result["duplicate"] is True
        assert first.audit.result["refund_id"] == second.audit.result["refund_id"]

    def test_ticket_ids_advance_within_one_agent(self, agent):
        first = agent.handle("我要投诉，请转人工", customer_id="CUST-001")
        second = agent.handle("我要投诉，请转人工", customer_id="CUST-001")
        assert (first.audit.result["ticket_id"], second.audit.result["ticket_id"]) == (
            "TKT-0001",
            "TKT-0002",
        )


class TestIntentClassification:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("请查询订单 ORD-1001 的物流状态", Intent.ORDER_STATUS),
            ("What is the status of order ORD-1001?", Intent.ORDER_STATUS),
            ("订单 ORD-1002 商品损坏，我要退款", Intent.REFUND),
            ("I want a refund for ORD-1002", Intent.REFUND),
            ("我要投诉，请转人工", Intent.HUMAN_HANDOFF),
            ("please connect me to a human agent", Intent.HUMAN_HANDOFF),
            ("今天天气怎么样", Intent.UNKNOWN),
        ],
    )
    def test_classify_intent(self, message, expected):
        assert EcommerceSupportAgent.classify_intent(message) is expected

    def test_refund_wins_over_status_when_both_keyword_groups_appear(self):
        message = "订单 ORD-1001 状态有问题，我要退款"
        assert EcommerceSupportAgent.classify_intent(message) is Intent.REFUND

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("订单 ORD-1001 状态", "ORD-1001"),
            ("ord-2001 到哪了", "ORD-2001"),
            ("帮我查一下", None),
            ("ORD-12 太短了", None),
        ],
    )
    def test_extract_order_id(self, message, expected):
        assert EcommerceSupportAgent.extract_order_id(message) == expected


class TestRejectedRequests:
    """请求级校验发生在规划之前，因此不能被记成上一轮的规划结果。"""

    @pytest.mark.parametrize(
        ("message", "customer_id", "expected_reason"),
        [
            ("   ", "CUST-001", "message is required"),
            ("\t\t", "CUST-001", "message is required"),
            ("订单 ORD-1001 状态", "", "customer_id is required"),
        ],
    )
    def test_pre_plan_rejection_names_the_missing_field(
        self, message, customer_id, expected_reason
    ):
        assert pre_plan_rejection(message, customer_id) == expected_reason

    def test_a_plannable_request_is_not_rejected(self):
        assert pre_plan_rejection("订单 ORD-1001 状态", "CUST-001") is None

    def test_a_rejected_request_never_reuses_the_previous_plan(self, agent):
        agent.handle("查询订单 ORD-1001 的状态", customer_id="CUST-001")
        assert agent.last_plan is not None

        response = agent.handle("      ", customer_id="CUST-001")

        assert response.state is AgentState.VALIDATION_FAILED
        assert response.audit.tool_name is None
        assert agent.last_plan is None

    def test_a_blank_customer_id_also_clears_the_plan(self, agent):
        agent.handle("查询订单 ORD-1001 的状态", customer_id="CUST-001")
        response = agent.handle("查询订单 ORD-1002 的状态", customer_id="  ")
        assert response.audit.error["code"] == ErrorCode.VALIDATION_ERROR
        assert agent.last_plan is None
