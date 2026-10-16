"""Fault injection: the scaffolding behind SPEC-FAULT-1..6.

Nothing in the production path imports this module. A case declares a fault, the runner
installs it for that one case, and the wrapper turns a dependency failure into exactly one
of the frozen contracts. The product code never learns that it is being tested -- there is
no "if evaluating:" branch anywhere, on purpose: a switch like that makes the result
depend on whoever remembers to turn it off (SPEC-FAULT-1).

Mode -> clause:

    tool + timeout       -> ToolTimeout          (SPEC-FAULT-2)
    tool + unavailable   -> ToolUnavailable      (SPEC-FAULT-3)
    retrieval + empty    -> hits = [], success   (SPEC-FAULT-5)
    retrieval + timeout  -> ToolTimeout          (SPEC-FAULT-2)
    model + rate_limit   -> LLMUnavailableError  (SPEC-FAULT-4 -> llm-fallback)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .llm.base import ChatMessage, LLMResponse, LLMUnavailableError
from .models import ToolResult
from .order_store import OrderStore
from .pipeline import V3Agent
from .planner import GroundedLLMPlanner, Planner
from .retrieval import KnowledgeBase
from .session import SessionStore
from .tools import ToolTimeout, ToolUnavailable

#: 可注入的工具名 -> V3Agent 上的属性名。键用的是**审计里的 tool_name**，
#: 这样用例文件里的声明与报告里的 `tool` 字段是同一套词汇，不需要第二张对照表。
TOOL_ATTRIBUTES: dict[str, str] = {
    "query_order_status": "order_status_tool",
    "apply_refund": "refund_tool",
    "create_human_ticket": "human_ticket_tool",
}

SEARCH_TOOL = "search_knowledge_base"

MODES_BY_TARGET: dict[str, tuple[str, ...]] = {
    "tool": ("timeout", "unavailable"),
    "retrieval": ("empty", "timeout", "unavailable"),
    "model": ("rate_limit",),
}


class FaultSpecError(ValueError):
    """An unusable fault declaration.

    声明写错时必须炸掉，而不是「什么都没注入、用例照样通过」——后者会让整份故障报告
    变成一份「一切正常」的假证据。
    """


@dataclass(frozen=True)
class FaultSpec:
    target: str
    mode: str
    tool_name: str | None = None

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> "FaultSpec":
        target = raw.get("target")
        mode = raw.get("mode")
        if target not in MODES_BY_TARGET:
            raise FaultSpecError(
                f"未知的 fault.target: {target!r}（允许：{sorted(MODES_BY_TARGET)}）"
            )
        if mode not in MODES_BY_TARGET[target]:
            raise FaultSpecError(
                f"fault.target={target!r} 不允许 mode={mode!r}"
                f"（允许：{MODES_BY_TARGET[target]}）"
            )
        tool_name = raw.get("tool_name")
        if target == "retrieval":
            if tool_name not in (None, SEARCH_TOOL):
                raise FaultSpecError(f"检索故障的 tool_name 只能是 {SEARCH_TOOL}")
            tool_name = SEARCH_TOOL
        elif target == "tool":
            if tool_name not in TOOL_ATTRIBUTES:
                raise FaultSpecError(
                    f"未知的 tool_name: {tool_name!r}（允许：{sorted(TOOL_ATTRIBUTES)}）"
                )
        elif tool_name is not None:
            raise FaultSpecError("model 故障不接受 tool_name")
        return cls(target, mode, tool_name)

    def to_dict(self) -> dict[str, Any]:
        return {"target": self.target, "mode": self.mode, "tool_name": self.tool_name}


class FaultyTool:
    """A tool whose only behaviour is the declared fault.

    ``name`` 直接透传内层工具：故障不许改名，否则审计里的 ``tool_name`` 会漂移，
    「哪个工具坏了」就没法从报告里读出来。
    """

    def __init__(self, inner: Any, fault: FaultSpec) -> None:
        self.inner = inner
        self.fault = fault

    @property
    def name(self) -> str:
        return self.inner.name

    def execute(self, **arguments: Any) -> ToolResult:
        name = self.inner.name
        if self.fault.mode == "timeout":
            raise ToolTimeout(f"{name} 超时（fault 注入）", tool_name=name)
        if self.fault.mode == "unavailable":
            raise ToolUnavailable(f"{name} 返回 5xx（fault 注入）", tool_name=name)
        if self.fault.mode == "empty":
            # SPEC-FAULT-5：空检索必须与「知识库里真没有」在行为上完全一样，所以这里
            # 返回的是一次**正常成功**的调用，只是 hits 为空。它既不是故障也不是错误。
            return ToolResult.ok(
                query=arguments.get("query"),
                top_k=arguments.get("top_k"),
                hits=[],
                retrieved=0,
            )
        raise FaultSpecError(f"未实现的 mode: {self.fault.mode!r}")


class RateLimitedClient:
    """An LLM client that always reports 429, and counts how often it did.

    SPEC-FAULT-4 要求降级**并且**把限流次数报出来：只有当「降级发生了多少次」可见时，
    「降级之后分数没掉」才算证据，否则它只是「这次没撞上」。
    """

    def __init__(self, model_id: str = "deepseek-flash") -> None:
        self._model_id = model_id
        self.rate_limited = 0

    @property
    def model_id(self) -> str:
        return self._model_id

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        self.rate_limited += 1
        raise LLMUnavailableError("HTTP 429 Too Many Requests（fault 注入）")


def build_faulted_agent(
    fault: FaultSpec | Mapping[str, Any],
    *,
    store: OrderStore | None = None,
    knowledge_base: KnowledgeBase | None = None,
    sessions: SessionStore | None = None,
    planner: Planner | None = None,
) -> V3Agent:
    """Build one v3 agent with exactly one fault installed.

    注入点刻意选在**工具对象**上而不是「agent 内部加开关」：工具是唯一被替换的东西，
    规划、编排、审计、判分全部走真实代码，所以故障用例判出来的行为就是生产行为。

    ``planner`` 只给 ``--llm`` 的 trace 运行用：让故障轮也走真实模型，好让报告里的
    ``llm.plan`` span 是真的。**模型类故障会忽略它**——那一轮的 client 就是被限流的那一个，
    换成别的就等于把被测对象换掉了。

    返回值上会挂两个诊断字段（脚手架专用，不是产品契约）：

    * ``agent.fault`` —— 本次注入的 :class:`FaultSpec`
    * ``agent.fault_client`` —— 模型故障时的 :class:`RateLimitedClient`（可读 ``rate_limited``）
    """
    spec = fault if isinstance(fault, FaultSpec) else FaultSpec.parse(fault)
    kwargs: dict[str, Any] = {}
    if store is not None:
        kwargs["store"] = store
    if knowledge_base is not None:
        kwargs["knowledge_base"] = knowledge_base
    if sessions is not None:
        kwargs["sessions"] = sessions

    if planner is not None and spec.target != "model":
        kwargs["planner"] = planner

    client: RateLimitedClient | None = None
    if spec.target == "model":
        client = RateLimitedClient()
        kwargs["planner"] = GroundedLLMPlanner(client)

    agent = V3Agent(**kwargs)
    if spec.target == "tool":
        assert spec.tool_name is not None
        attribute = TOOL_ATTRIBUTES[spec.tool_name]
        setattr(agent.agent, attribute, FaultyTool(getattr(agent.agent, attribute), spec))
    elif spec.target == "retrieval":
        agent.knowledge_tool = FaultyTool(agent.knowledge_tool, spec)

    agent.fault = spec  # type: ignore[attr-defined]
    if client is not None:
        agent.fault_client = client  # type: ignore[attr-defined]
    return agent


def install_on_tool(tool: Any, fault: FaultSpec | Mapping[str, Any]) -> FaultyTool:
    """Wrap one tool object; used by tests that want a fault without a whole agent."""
    spec = fault if isinstance(fault, FaultSpec) else FaultSpec.parse(fault)
    return FaultyTool(tool, spec)


__all__ = [
    "MODES_BY_TARGET",
    "SEARCH_TOOL",
    "TOOL_ATTRIBUTES",
    "FaultSpec",
    "FaultSpecError",
    "FaultyTool",
    "RateLimitedClient",
    "build_faulted_agent",
    "install_on_tool",
]
