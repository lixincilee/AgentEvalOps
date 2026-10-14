"""E-commerce customer-support agent: validate, plan, dispatch one tool."""

from __future__ import annotations

from typing import Any

from .models import AgentResponse, AgentState, AuditRecord, ErrorCode, Intent, ToolResult
from .order_store import OrderStore
from .planner import Plan, Planner, RulePlanner, classify_intent, extract_order_id
from .tools import HumanTicketTool, OrderStatusTool, RefundTool


class EcommerceSupportAgent:
    """Validates the request, plans it, then dispatches exactly one tool.

    The planning layer is injectable: pass an ``LLMPlanner`` to run the model-driven
    version. Everything downstream -- states, tool names, audit fields -- is byte-for-byte
    identical, which is what makes v1 and v2 directly comparable.
    """

    def __init__(
        self, store: OrderStore | None = None, *, planner: Planner | None = None
    ) -> None:
        self.store = store or OrderStore()
        self.planner: Planner = planner or RulePlanner()
        self.order_status_tool = OrderStatusTool(self.store)
        self.refund_tool = RefundTool(self.store)
        self.human_ticket_tool = HumanTicketTool(self.store)
        self.last_plan: Plan | None = None

    @staticmethod
    def classify_intent(message: str) -> Intent:
        """Kept for backwards compatibility; delegates to the v1 rule planner."""
        return classify_intent(message)

    @staticmethod
    def extract_order_id(message: str) -> str | None:
        return extract_order_id(message)

    def handle(self, message: str, *, customer_id: str) -> AgentResponse:
        if not isinstance(message, str) or not message.strip():
            return self._validation_error(Intent.UNKNOWN, None, {}, "message is required")
        if not isinstance(customer_id, str) or not customer_id.strip():
            return self._validation_error(
                Intent.UNKNOWN, None, {}, "customer_id is required"
            )

        plan = self.planner.plan(message)
        self.last_plan = plan
        intent, order_id = plan.intent, plan.order_id

        if intent is Intent.UNKNOWN:
            return self._no_action(intent)
        if intent is Intent.ORDER_STATUS:
            tool = self.order_status_tool
            if order_id is None:
                return self._validation_error(
                    intent, tool.name, {"customer_id": customer_id}, "order_id is required"
                )
            arguments = {"order_id": order_id, "customer_id": customer_id}
            return self._execute(intent, tool.name, arguments, tool.execute(**arguments))
        if intent is Intent.REFUND:
            tool = self.refund_tool
            if order_id is None:
                return self._validation_error(
                    intent, tool.name, {"customer_id": customer_id}, "order_id is required"
                )
            arguments = {
                "order_id": order_id,
                "customer_id": customer_id,
                "reason": message.strip(),
            }
            return self._execute(intent, tool.name, arguments, tool.execute(**arguments))

        tool = self.human_ticket_tool
        arguments: dict[str, Any] = {
            "customer_id": customer_id,
            "summary": message.strip(),
            "order_id": order_id,
        }
        return self._execute(intent, tool.name, arguments, tool.execute(**arguments))

    @staticmethod
    def _execute(
        intent: Intent, tool_name: str, arguments: dict[str, Any], outcome: ToolResult
    ) -> AgentResponse:
        if outcome.success:
            state = AgentState.TOOL_COMPLETED
            audit = AuditRecord(intent, state, tool_name, arguments, outcome.data, None)
            return AgentResponse("request completed", intent, state, audit)
        state = AgentState.TOOL_FAILED
        error = {"code": outcome.error_code, "message": outcome.error_message}
        audit = AuditRecord(intent, state, tool_name, arguments, None, error)
        return AgentResponse(outcome.error_message or "tool failed", intent, state, audit)

    @staticmethod
    def _validation_error(
        intent: Intent, tool_name: str | None, arguments: dict[str, Any], message: str
    ) -> AgentResponse:
        state = AgentState.VALIDATION_FAILED
        error = {"code": ErrorCode.VALIDATION_ERROR, "message": message}
        audit = AuditRecord(intent, state, tool_name, arguments, None, error)
        return AgentResponse(message, intent, state, audit)

    @staticmethod
    def _no_action(intent: Intent) -> AgentResponse:
        state = AgentState.NO_ACTION
        error = {
            "code": ErrorCode.UNSUPPORTED_INTENT,
            "message": "intent is not supported",
        }
        audit = AuditRecord(intent, state, None, {}, None, error)
        return AgentResponse(error["message"], intent, state, audit)

