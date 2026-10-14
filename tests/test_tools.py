"""Unit tests for the three deterministic tools, including boundary behaviour."""

from __future__ import annotations

import pytest

from agentevalops.models import ErrorCode, ToolResult
from agentevalops.order_store import OrderStore
from agentevalops.tools import RefundTool


class TestOrderStatusToolSuccess:
    def test_returns_status_and_tracking_number_for_an_owned_order(self, status_tool):
        result = status_tool.execute(order_id="ORD-1001", customer_id="CUST-001")
        assert isinstance(result, ToolResult)
        assert result.success is True
        assert result.error_code is None
        assert result.error_message is None
        assert result.data == {
            "order_id": "ORD-1001",
            "status": "shipped",
            "tracking_number": "SF1234567890",
        }

    def test_returns_null_tracking_number_when_the_order_has_none(self, status_tool):
        result = status_tool.execute(order_id="ORD-1003", customer_id="CUST-001")
        assert result.success is True
        assert result.data["status"] == "cancelled"
        assert result.data["tracking_number"] is None


class TestOrderStatusToolValidation:
    @pytest.mark.parametrize("order_id", ["", "   ", None, 42])
    def test_rejects_blank_or_non_string_order_id(self, status_tool, order_id):
        result = status_tool.execute(order_id=order_id, customer_id="CUST-001")
        assert result.success is False
        assert result.error_code == ErrorCode.INVALID_ARGUMENT
        assert result.data == {}

    @pytest.mark.parametrize("customer_id", ["", "   ", None])
    def test_rejects_blank_customer_id(self, status_tool, customer_id):
        result = status_tool.execute(order_id="ORD-1001", customer_id=customer_id)
        assert result.success is False
        assert result.error_code == ErrorCode.INVALID_ARGUMENT


class TestOrderStatusToolAuthorisation:
    def test_hides_orders_owned_by_another_customer(self, status_tool):
        result = status_tool.execute(order_id="ORD-2001", customer_id="CUST-001")
        assert result.success is False
        assert result.error_code == ErrorCode.ORDER_NOT_FOUND
        assert result.data == {}

    def test_foreign_and_missing_orders_are_indistinguishable(self, status_tool):
        foreign = status_tool.execute(order_id="ORD-2001", customer_id="CUST-001")
        missing = status_tool.execute(order_id="ORD-9999", customer_id="CUST-001")
        assert foreign.error_code == missing.error_code
        assert foreign.error_message == missing.error_message


class TestRefundToolSuccess:
    def test_creates_a_submitted_request_for_a_delivered_order(self, refund_tool):
        result = refund_tool.execute(
            order_id="ORD-1002", customer_id="CUST-001", reason="商品损坏"
        )
        assert result.success is True
        assert result.data == {
            "refund_id": "REF-0001",
            "order_id": "ORD-1002",
            "status": "submitted",
            "duplicate": False,
        }

    def test_ids_increment_across_distinct_orders(self):
        store = OrderStore(
            [
                {"order_id": "ORD-A", "customer_id": "CUST-001", "status": "delivered"},
                {"order_id": "ORD-B", "customer_id": "CUST-001", "status": "delivered"},
            ]
        )
        tool = RefundTool(store)
        first = tool.execute(order_id="ORD-A", customer_id="CUST-001", reason="r")
        second = tool.execute(order_id="ORD-B", customer_id="CUST-001", reason="r")
        assert first.data["refund_id"] == "REF-0001"
        assert second.data["refund_id"] == "REF-0002"

    def test_repeating_a_refund_is_idempotent(self, refund_tool):
        first = refund_tool.execute(order_id="ORD-1002", customer_id="CUST-001", reason="a")
        second = refund_tool.execute(order_id="ORD-1002", customer_id="CUST-001", reason="b")
        assert second.success is True
        assert second.data["duplicate"] is True
        assert second.data["refund_id"] == first.data["refund_id"]


class TestRefundToolRejections:
    @pytest.mark.parametrize("order_id", ["ORD-1001", "ORD-1003"])
    def test_rejects_non_refundable_statuses(self, refund_tool, order_id):
        result = refund_tool.execute(order_id=order_id, customer_id="CUST-001", reason="r")
        assert result.success is False
        assert result.error_code == ErrorCode.REFUND_NOT_ALLOWED
        assert result.data == {}

    def test_error_message_names_the_blocking_status(self, refund_tool):
        result = refund_tool.execute(order_id="ORD-1001", customer_id="CUST-001", reason="r")
        assert "shipped" in (result.error_message or "")

    @pytest.mark.parametrize("reason", ["", "   ", None])
    def test_rejects_blank_reason(self, refund_tool, reason):
        result = refund_tool.execute(order_id="ORD-1002", customer_id="CUST-001", reason=reason)
        assert result.error_code == ErrorCode.INVALID_ARGUMENT

    @pytest.mark.parametrize("order_id", ["ORD-2001", "ORD-9999"])
    def test_rejects_foreign_or_unknown_orders(self, refund_tool, order_id):
        result = refund_tool.execute(order_id=order_id, customer_id="CUST-001", reason="r")
        assert result.success is False
        assert result.error_code == ErrorCode.ORDER_NOT_FOUND

    def test_accepts_a_reason_of_exactly_the_length_limit(self, refund_tool):
        result = refund_tool.execute(
            order_id="ORD-1002", customer_id="CUST-001", reason="x" * 500
        )
        assert result.success is True

    def test_rejects_a_reason_one_character_over_the_limit(self, refund_tool):
        result = refund_tool.execute(
            order_id="ORD-1002", customer_id="CUST-001", reason="x" * 501
        )
        assert result.success is False
        assert result.error_code == ErrorCode.INVALID_ARGUMENT


class TestHumanTicketTool:
    def test_creates_an_open_ticket_linked_to_an_owned_order(self, ticket_tool):
        result = ticket_tool.execute(
            customer_id="CUST-001", summary="物流异常", order_id="ORD-1001"
        )
        assert result.success is True
        assert result.data == {
            "ticket_id": "TKT-0001",
            "status": "open",
            "customer_id": "CUST-001",
            "order_id": "ORD-1001",
        }

    def test_creates_a_ticket_without_an_order(self, ticket_tool):
        result = ticket_tool.execute(customer_id="CUST-001", summary="我要投诉")
        assert result.success is True
        assert result.data["order_id"] is None
        assert result.data["ticket_id"] == "TKT-0001"

    def test_ticket_ids_increment_per_call(self, ticket_tool):
        first = ticket_tool.execute(customer_id="CUST-001", summary="a")
        second = ticket_tool.execute(customer_id="CUST-001", summary="b")
        assert (first.data["ticket_id"], second.data["ticket_id"]) == ("TKT-0001", "TKT-0002")

    @pytest.mark.parametrize("order_id", ["ORD-2001", "ORD-9999"])
    def test_rejects_foreign_or_unknown_orders(self, ticket_tool, order_id):
        result = ticket_tool.execute(customer_id="CUST-001", summary="x", order_id=order_id)
        assert result.success is False
        assert result.error_code == ErrorCode.ORDER_NOT_FOUND

    @pytest.mark.parametrize("summary", ["", "   ", None])
    def test_rejects_blank_summary(self, ticket_tool, summary):
        result = ticket_tool.execute(customer_id="CUST-001", summary=summary)
        assert result.error_code == ErrorCode.INVALID_ARGUMENT

    @pytest.mark.parametrize("customer_id", ["", "   ", None])
    def test_rejects_blank_customer_id(self, ticket_tool, customer_id):
        result = ticket_tool.execute(customer_id=customer_id, summary="x")
        assert result.error_code == ErrorCode.INVALID_ARGUMENT

    def test_accepts_a_summary_of_exactly_the_length_limit(self, ticket_tool):
        result = ticket_tool.execute(customer_id="CUST-001", summary="x" * 1000)
        assert result.success is True

    def test_rejects_a_summary_one_character_over_the_limit(self, ticket_tool):
        result = ticket_tool.execute(customer_id="CUST-001", summary="x" * 1001)
        assert result.success is False
        assert result.error_code == ErrorCode.INVALID_ARGUMENT
