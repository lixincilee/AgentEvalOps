"""E-commerce customer-support agent: validate, plan, dispatch one tool."""

from __future__ import annotations

from typing import Any

from .models import AgentResponse, AgentState, AuditRecord, ErrorCode, Intent, ToolResult
from .order_store import OrderStore
from .planner import Plan, Planner, RulePlanner, classify_intent, extract_order_id
from .tools import HumanTicketTool, OrderStatusTool, RefundTool, ToolError
from .tracing import SpanKind, span


def pre_plan_rejection(message: Any, customer_id: Any) -> str | None:
    """Return why a request never reaches the planner, or ``None`` when it does.

    A blank message or a missing customer id is *request* validation: there is no intent
    to infer and no order to look up, so spending a model call on it is pure waste.
    The comparison harness calls this same function to keep its scripted answers aligned
    with the calls the agent actually makes; if the two ever disagree, every later
    scripted answer shifts by one and the zero-cost self-check starts reporting fiction.
    """
    if not isinstance(message, str) or not message.strip():
        return "message is required"
    if not isinstance(customer_id, str) or not customer_id.strip():
        return "customer_id is required"
    return None


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
        # Clear the previous plan first. A request rejected before planning must never be
        # attributed to the previous call's decision: that silently mislabels traces and
        # made the 165-case report claim source=llm for cases that made no model call.
        self.last_plan = None
        rejection = pre_plan_rejection(message, customer_id)
        if rejection is not None:
            return self._validation_error(Intent.UNKNOWN, None, {}, rejection)

        plan = self.planner.plan(message)
        self.last_plan = plan
        return self.dispatch(
            plan.intent, plan.order_id, message, customer_id=customer_id
        )

    def dispatch(
        self, intent: Intent, order_id: str | None, message: str, *, customer_id: str
    ) -> AgentResponse:
        """Turn one plan into exactly one tool call: the deterministic core of the agent.

        The v3 graph calls this from its own ``dispatch`` node. There is deliberately only
        one copy: if the graph re-implemented dispatch, every later v2/v3 difference could
        be implementation drift instead of a real design difference (SPEC-GRAPH-1).
        """

        if intent is Intent.UNKNOWN:
            return self._no_action(intent)
        if intent is Intent.ORDER_STATUS:
            tool = self.order_status_tool
            if order_id is None:
                return self._validation_error(
                    intent, tool.name, {"customer_id": customer_id}, "order_id is required"
                )
            arguments = {"order_id": order_id, "customer_id": customer_id}
            return self._execute(intent, tool.name, arguments, self._call_tool(tool, arguments))
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
            return self._execute(intent, tool.name, arguments, self._call_tool(tool, arguments))

        if intent is Intent.HUMAN_HANDOFF:
            tool = self.human_ticket_tool
            arguments: dict[str, Any] = {
                "customer_id": customer_id,
                "summary": message.strip(),
                "order_id": order_id,
            }
            return self._execute(intent, tool.name, arguments, self._call_tool(tool, arguments))

        # v1.1 的 policy_qa 需要检索能力，这个基线 Agent 没有。它必须明确地「不支持」，
        # 而不是掉进末尾分支去建工单——那是靠分支顺序意外得到的正确行为，改一次就崩。
        return self._no_action(intent)

    @staticmethod
    def _call_tool(tool: Any, arguments: dict[str, Any]) -> ToolResult:
        """Call one tool, turning *declared* dependency faults into a ToolResult.

        SPEC-FAULT-6：只有 ToolError 的子类会被转成 tool_failed。其它异常必须继续往上抛，
        因为那是 bug 不是故障 —— 在这一层铺一个 ``except Exception``，等于把所有真实缺陷
        都伪装成「工具今天有点抖」。

        这里同时是工具层**唯一**的插桩点：入参、结果、错误码都记在同一段逻辑里，
        不会再出现「审计说调了工具、trace 说没调」这种两套记录对不上的情况。
        """
        tool_name = str(getattr(tool, "name", type(tool).__name__))
        with span(
            f"tool.{tool_name}",
            kind=SpanKind.TOOL,
            tool=tool_name,
            arguments=arguments,
        ) as tool_span:
            try:
                outcome = tool.execute(**arguments)
            except ToolError as fault:
                tool_span.fail(fault.error_code, str(fault))
                return ToolResult.fail(fault.error_code, str(fault))
            tool_span.set(
                success=outcome.success,
                error_code=outcome.error_code,
                result=outcome.data,
            )
            return outcome

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

