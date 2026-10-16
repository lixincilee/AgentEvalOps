"""v3: the LangGraph orchestration around the same agent, tools and audit contract.

SPEC-GRAPH-1 / 2 / 3. The graph exists to make each stage *separately judgeable*: a
retrieval miss, a planning mistake and a tool failure must not look the same in a report.
The AuditRecord is deliberately untouched -- retrieval evidence travels in
:class:`~agentevalops.models.RetrievalRecord`, never inside the audit.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .agent import EcommerceSupportAgent, pre_plan_rejection
from .models import (
    AgentResponse,
    AgentState,
    AuditRecord,
    ErrorCode,
    Intent,
    RetrievalRecord,
    SlotRecord,
    ToolResult,
)
from .order_store import OrderStore
from .planner import Plan, Planner, RulePlannerV3, needs_retrieval
from .retrieval import DEFAULT_TOP_K, KnowledgeBase, KnowledgeBaseTool
from .session import SLOT_INTENTS, SessionStore, resolve_slot
from .tools import ToolError
from .tracing import SpanKind, Tracer, current_tracer, span, tracing

#: 检索工具名。抽成常量，免得审计里的 tool_name 在各处拼写不一致。
TOOL_SEARCH = "search_knowledge_base"


class GraphState(TypedDict, total=False):
    """节点之间传的中间态。

    刻意与 ``AuditRecord`` 分开：审计是**对外契约**（三个版本必须吐出同一种记录），
    中间态是**内部实现**（v3 才有检索与会话）。改中间态不影响可比性，改审计才会。
    """

    message: str
    customer_id: str
    session_id: str | None
    session_hint: str | None
    previous_order_id: str | None
    retrieval_needed: bool
    retrieval: RetrievalRecord | None
    retrieval_result: ToolResult | None
    retrieval_fault: ToolError | None
    plan: Plan | None
    slot: SlotRecord | None
    response: AgentResponse | None


class V3Agent:
    """v3 门面：handle() 的签名与 v1/v2 一致，外加一个可选的 session_id。

    SPEC-MT-1：不带 session_id 时行为必须与 v1.0 **完全一致**。所以会话是可选分支，
    而不是默认路径——否则每一条既有回归用例都会被多轮状态悄悄改写。
    """

    name = "v3"
    #: SPEC-GRAPH-2 规定的节点顺序；测试直接断言它。
    NODE_ORDER = ("validate", "retrieve", "plan", "dispatch", "respond")

    def __init__(
        self,
        store: OrderStore | None = None,
        *,
        planner: Planner | None = None,
        knowledge_base: KnowledgeBase | None = None,
        sessions: SessionStore | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.store = store or OrderStore()
        # 默认用规则臂：离线可复现。真实评测跑的是 GroundedLLMPlanner（见 scripts）。
        self.agent = EcommerceSupportAgent(
            self.store, planner=planner or RulePlannerV3()
        )
        self.knowledge_base = knowledge_base or KnowledgeBase()
        self.knowledge_tool = KnowledgeBaseTool(self.knowledge_base)
        self.sessions = sessions or SessionStore()
        #: 显式注入的 tracer；没注入时看上下文里有没有激活的 tracer（见 handle）。
        self.tracer = tracer
        self.last_plan: Plan | None = None
        self.last_retrieval: RetrievalRecord | None = None
        self.last_slot: SlotRecord | None = None
        self.last_nodes: list[str] = []
        self.graph = self._build_graph()

    @property
    def planner(self) -> Planner:
        return self.agent.planner

    # --- 图 ------------------------------------------------------------------

    def _build_graph(self):
        graph = StateGraph(GraphState)
        graph.add_node("validate", self._traced("validate", self._validate_node))
        graph.add_node("retrieve", self._traced("retrieve", self._retrieve_node))
        graph.add_node("plan", self._traced("plan", self._plan_node))
        graph.add_node("dispatch", self._traced("dispatch", self._dispatch_node))
        graph.add_node("respond", self._traced("respond", self._respond_node))
        graph.add_edge(START, "validate")
        # 条件边就是 SPEC-RAG-2 说的「只有 policy_qa 才检索」。判断用的是确定性判据，
        # 因为 retrieve 排在 plan 之前——那时还没有意图可看（SPEC-RAG-7）。
        graph.add_conditional_edges(
            "validate",
            self._after_validate,
            {"retrieve": "retrieve", "plan": "plan", "respond": "respond"},
        )
        graph.add_edge("retrieve", "plan")
        graph.add_edge("plan", "dispatch")
        graph.add_edge("dispatch", "respond")
        graph.add_edge("respond", END)
        return graph.compile()

    def _traced(
        self, name: str, node: Callable[[GraphState], GraphState]
    ) -> Callable[[GraphState], GraphState]:
        """记下实际走过的节点。SPEC-GRAPH-2 的「短路不得经过 retrieve / plan」
        必须能被直接断言，而不是靠读代码相信。

        M8 起顺带开一个节点 span：节点名 + 这一步的结局（意图 / 状态 / 错误码）。
        失败定位的第一层就是它——先看哪个节点先出事，再往下钻它的子 span。
        """

        def runner(state: GraphState) -> GraphState:
            self.last_nodes.append(name)
            with span(f"node.{name}", kind=SpanKind.NODE, node=name) as node_span:
                result = node(state)
            if not isinstance(result, dict):
                return result
            response = result.get("response")
            if response is not None:
                error = response.audit.error or {}
                node_span.set(
                    intent=str(response.intent),
                    state=str(response.state),
                    tool_name=response.audit.tool_name,
                    error_code=error.get("code"),
                )
            fault = result.get("retrieval_fault")
            if fault is not None:
                node_span.fail(
                    getattr(fault, "error_code", type(fault).__name__), str(fault)
                )
            record = result.get("retrieval")
            if record is not None:
                node_span.set(
                    retrieval_query=record.query,
                    retrieval_hits=len(record.hits),
                    retrieval_grounded=record.grounded,
                )
            return result

        return runner

    def _after_validate(self, state: GraphState) -> str:
        if state.get("response") is not None:
            return "respond"
        return "retrieve" if state.get("retrieval_needed") else "plan"

    # --- 节点 ----------------------------------------------------------------

    def _validate_node(self, state: GraphState) -> GraphState:
        """SPEC-REQ-1：请求级校验。空消息 / 缺 customer_id 到此为止。"""
        rejection = pre_plan_rejection(state["message"], state["customer_id"])
        if rejection is None:
            return {}
        return {
            "response": EcommerceSupportAgent._validation_error(
                Intent.UNKNOWN, None, {}, rejection
            )
        }

    def _retrieve_node(self, state: GraphState) -> GraphState:
        """只在条件边放行时才被调用，所以这里不必再判断一次该不该检索。

        检索 span 建在**节点里**而不是 ``KnowledgeBaseTool`` 里：故障注入是包在工具外面的
        （``FaultyTool`` 在调内层之前就抛），插在工具内部的话，「检索工具坏了」那一轮的
        trace 里会一条检索痕迹都不剩——而它恰恰是最需要被看见的轮次。
        """
        query = state["message"].strip()
        with span(
            "retriever.search",
            kind=SpanKind.RETRIEVER,
            tool=self.knowledge_tool.name,
            query=query,
            top_k=DEFAULT_TOP_K,
        ) as search_span:
            try:
                outcome = self.knowledge_tool.execute(query=query, top_k=DEFAULT_TOP_K)
            except ToolError as fault:
                # SPEC-FAULT-2 / 3：检索工具也会坏，而「链路坏了」和「知识库里没有」必须是
                # 两种结果（SPEC-RAG-1）。这里只把故障记下来，流程照常走到 plan —— 意图仍然
                # 要判出来，报告才分得清「检索坏了」和「这一轮本来就不该检索」。
                search_span.fail(fault.error_code, str(fault))
                return {"retrieval_fault": fault}
            hits = tuple(outcome.data.get("hits", ()))
            search_span.set(
                hits=len(hits),
                doc_ids=[hit.get("doc_id") for hit in hits],
                scores=[hit.get("score") for hit in hits],
                expired=[hit.get("expired") for hit in hits],
                grounded=bool(hits),
            )
            return {
                "retrieval_result": outcome,
                "retrieval": RetrievalRecord(
                    query=query, top_k=DEFAULT_TOP_K, hits=hits, grounded=bool(hits)
                ),
            }

    def _plan_node(self, state: GraphState) -> GraphState:
        plan = self.planner.plan(
            state["message"], session_hint=state.get("session_hint")
        )
        # 槽位只在本轮意图是业务动作时解析（SPEC-MT-2）。policy_qa 与 unknown 不消费
        # 订单号，给它们记一个槽位只会让「这一轮到底用了什么槽位」变得说不清——
        # 槽位记录的价值就在于它是**实际被用掉的那个**。
        if plan.intent not in SLOT_INTENTS:
            return {"plan": plan, "slot": SlotRecord()}
        order_id, source = resolve_slot(
            state["message"],
            planned=plan.order_id,
            previous_order_id=state.get("previous_order_id"),
        )
        return {
            "plan": plan,
            "slot": SlotRecord(order_id=order_id, source=source),
        }

    def _dispatch_node(self, state: GraphState) -> GraphState:
        plan = state["plan"]
        slot = state["slot"]
        if plan.intent is Intent.POLICY_QA:
            return {"response": self._policy_response(state)}
        return {
            "response": self.agent.dispatch(
                plan.intent,
                slot.order_id,
                state["message"],
                customer_id=state["customer_id"],
            )
        }

    def _policy_response(self, state: GraphState) -> AgentResponse:
        """SPEC-RAG-4：没有依据就拒答，绝不输出一段编出来的政策。"""
        arguments = {"query": state["message"].strip(), "top_k": DEFAULT_TOP_K}
        fault = state.get("retrieval_fault")
        if fault is not None:
            error = {"code": fault.error_code, "message": str(fault)}
            audit = AuditRecord(
                Intent.POLICY_QA,
                AgentState.TOOL_FAILED,
                TOOL_SEARCH,
                arguments,
                None,
                error,
            )
            return AgentResponse(str(fault), Intent.POLICY_QA, AgentState.TOOL_FAILED, audit)
        record = state.get("retrieval")
        hits = () if record is None else record.hits
        if not hits:
            error = {
                "code": ErrorCode.NO_KNOWLEDGE,
                "message": "no supporting document found in the knowledge base",
            }
            audit = AuditRecord(
                Intent.POLICY_QA, AgentState.NO_ACTION, None, arguments, None, error
            )
            return AgentResponse(error["message"], Intent.POLICY_QA, AgentState.NO_ACTION, audit)
        outcome = state["retrieval_result"]
        audit = AuditRecord(
            Intent.POLICY_QA,
            AgentState.TOOL_COMPLETED,
            TOOL_SEARCH,
            arguments,
            outcome.data,
            None,
        )
        return AgentResponse(
            "request completed", Intent.POLICY_QA, AgentState.TOOL_COMPLETED, audit
        )

    def _respond_node(self, state: GraphState) -> GraphState:
        """收尾：把本轮写进会话状态。只在响应定稿之后写（SPEC-MT-2）。"""
        response = state.get("response")
        session_id = state.get("session_id")
        if response is None or session_id is None:
            return {}
        slot = state.get("slot")
        if slot is None or slot.order_id is None:
            return {}
        session = self.sessions.state_for(session_id, state["customer_id"])
        with span(
            "state.update",
            kind=SpanKind.STATE,
            session_id=session_id,
            order_id=slot.order_id,
            source=slot.source,
        ) as state_span:
            session.remember(
                state["message"], response.intent, slot.order_id, slot.source
            )
            state_span.set(written=True, last_order_id=session.last_order_id)
        return {}

    # --- 门面 ----------------------------------------------------------------

    def _stamp(self, turn_span: Any, response: AgentResponse) -> AgentResponse:
        """把这一轮的结论记在根 span 上：报告先看它，再决定往哪一层钻。

        槽位与判定来源也在这里：多轮状态出问题时，证据就是 ``slot_source`` 是不是
        ``inherited``、以及这一轮的意图是模型判的还是兜底判的。这两件事都不在
        ``AuditRecord`` 里（审计是对外契约，不为观测扩容），所以只能记在 trace 上。
        """
        error = response.audit.error or {}
        slot = self.last_slot
        turn_span.set(
            intent=str(response.intent),
            state=str(response.state),
            tool_name=response.audit.tool_name,
            error_code=error.get("code"),
            response=response.message,
            plan_source=None if self.last_plan is None else self.last_plan.source,
            slot_order_id=None if slot is None else slot.order_id,
            slot_source=None if slot is None else slot.source,
            nodes=list(self.last_nodes),
        )
        return response

    def handle(
        self,
        message: str,
        *,
        customer_id: str,
        session_id: str | None = None,
    ) -> AgentResponse:
        """跑一轮。带 tracer 时留一条 trace；没带时行为与 M3 逐字节一致。"""
        tracer = self.tracer or current_tracer()
        if tracer is None:
            return self._handle(message, customer_id=customer_id, session_id=session_id)
        # 在上下文里激活 tracer：下游的 agent / tool / planner / session 就不必再传一遍
        # 参数，也能把子 span 挂到这一轮上。
        with tracing(tracer):
            metadata = {
                "message": message,
                "customer_id": customer_id,
                "session_id": session_id,
            }
            if tracer.active:
                # 已经在一轮里（调用方自己开了 turn）：只挂一个子 span，不另起一条 trace，
                # 否则一次调用会分裂成两条互不相干的记录。
                with tracer.span(
                    "agent.turn", kind=SpanKind.TURN, **metadata
                ) as turn_span:
                    response = self._handle(
                        message, customer_id=customer_id, session_id=session_id
                    )
                    return self._stamp(turn_span, response)
            with tracer.turn("agent.turn", **metadata) as root:
                response = self._handle(
                    message, customer_id=customer_id, session_id=session_id
                )
                return self._stamp(root, response)

    def _handle(
        self,
        message: str,
        *,
        customer_id: str,
        session_id: str | None = None,
    ) -> AgentResponse:
        self.last_plan = None
        self.last_retrieval = None
        self.last_slot = None
        self.last_nodes = []

        session = (
            None
            if session_id is None
            else self.sessions.state_for(session_id, customer_id)
        )
        initial: GraphState = {
            "message": message,
            "customer_id": customer_id,
            "session_id": session_id,
            "session_hint": None if session is None else session.hint(),
            "previous_order_id": None if session is None else session.last_order_id,
            "retrieval_needed": isinstance(message, str) and needs_retrieval(message),
        }
        final = dict(initial)
        final.update(self.graph.invoke(initial))
        self.last_plan = final.get("plan")
        self.last_retrieval = final.get("retrieval")
        self.last_slot = final.get("slot")
        return final["response"]
