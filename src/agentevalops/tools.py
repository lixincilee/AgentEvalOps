"""Deterministic tools exposed to the customer-support agent."""

from __future__ import annotations

from typing import Any

from .models import ErrorCode, ToolResult
from .order_store import OrderStore


def _missing(value: Any) -> bool:
    return not isinstance(value, str) or not value.strip()


class OrderStatusTool:
    name = "query_order_status"

    def __init__(self, store: OrderStore) -> None:
        self.store = store

    def execute(self, *, order_id: str, customer_id: str) -> ToolResult:
        if _missing(order_id) or _missing(customer_id):
            return ToolResult.fail(
                ErrorCode.INVALID_ARGUMENT, "order_id and customer_id are required"
            )
        order = self.store.get_owned(order_id, customer_id)
        if order is None:
            # The same response covers missing and foreign orders to prevent enumeration.
            return ToolResult.fail(
                ErrorCode.ORDER_NOT_FOUND, "order was not found for this customer"
            )
        return ToolResult.ok(
            order_id=order["order_id"],
            status=order["status"],
            tracking_number=order.get("tracking_number"),
        )


class RefundTool:
    name = "apply_refund"
    refundable_statuses = {"delivered"}

    def __init__(self, store: OrderStore) -> None:
        self.store = store
        self._requests: dict[tuple[str, str], dict[str, Any]] = {}
        self._sequence = 0

    def execute(self, *, order_id: str, customer_id: str, reason: str) -> ToolResult:
        if _missing(order_id) or _missing(customer_id) or _missing(reason):
            return ToolResult.fail(
                ErrorCode.INVALID_ARGUMENT,
                "order_id, customer_id, and reason are required",
            )
        if len(reason) > 500:
            return ToolResult.fail(
                ErrorCode.INVALID_ARGUMENT, "reason must be at most 500 characters"
            )

        order = self.store.get_owned(order_id, customer_id)
        if order is None:
            return ToolResult.fail(
                ErrorCode.ORDER_NOT_FOUND, "order was not found for this customer"
            )
        if order["status"] not in self.refundable_statuses:
            return ToolResult.fail(
                ErrorCode.REFUND_NOT_ALLOWED,
                f"orders with status '{order['status']}' are not refundable",
            )

        key = (order_id, customer_id)
        existing = self._requests.get(key)
        if existing:
            return ToolResult.ok(**existing, duplicate=True)

        self._sequence += 1
        request = {
            "refund_id": f"REF-{self._sequence:04d}",
            "order_id": order_id,
            "status": "submitted",
        }
        self._requests[key] = request
        return ToolResult.ok(**request, duplicate=False)


class HumanTicketTool:
    name = "create_human_ticket"

    def __init__(self, store: OrderStore) -> None:
        self.store = store
        self._sequence = 0

    def execute(
        self, *, customer_id: str, summary: str, order_id: str | None = None
    ) -> ToolResult:
        if _missing(customer_id) or _missing(summary):
            return ToolResult.fail(
                ErrorCode.INVALID_ARGUMENT, "customer_id and summary are required"
            )
        if len(summary) > 1000:
            return ToolResult.fail(
                ErrorCode.INVALID_ARGUMENT, "summary must be at most 1000 characters"
            )
        if order_id and self.store.get_owned(order_id, customer_id) is None:
            return ToolResult.fail(
                ErrorCode.ORDER_NOT_FOUND, "order was not found for this customer"
            )

        self._sequence += 1
        return ToolResult.ok(
            ticket_id=f"TKT-{self._sequence:04d}",
            status="open",
            customer_id=customer_id,
            order_id=order_id,
        )

