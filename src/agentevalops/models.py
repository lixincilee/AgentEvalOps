"""Shared data contracts for tools, agent responses, and audit records."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Intent(StrEnum):
    ORDER_STATUS = "order_status"
    REFUND = "refund"
    HUMAN_HANDOFF = "human_handoff"
    #: v1.1：指向平台通用规则 / 条款 / 时效 / 费用的知识性问题（SPEC-RAG-3）。
    #: 与「想办理某个订单的业务」区分开——后者仍按业务动作判意图。
    POLICY_QA = "policy_qa"
    UNKNOWN = "unknown"


class AgentState(StrEnum):
    RECEIVED = "received"
    VALIDATION_FAILED = "validation_failed"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    NO_ACTION = "no_action"


class ErrorCode(StrEnum):
    """Canonical error codes; the stable contract every tool must honour."""

    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    REFUND_NOT_ALLOWED = "REFUND_NOT_ALLOWED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNSUPPORTED_INTENT = "UNSUPPORTED_INTENT"
    #: v1.1：检索不到依据。**不是工具故障**，而是「知识库里没有」（SPEC-RAG-4）。
    NO_KNOWLEDGE = "NO_KNOWLEDGE"


@dataclass(frozen=True)
class ToolResult:
    """Stable return contract used by every tool."""

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def ok(cls, **data: Any) -> "ToolResult":
        return cls(success=True, data=data)

    @classmethod
    def fail(cls, code: str, message: str) -> "ToolResult":
        return cls(success=False, error_code=code, error_message=message)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: 槽位来源标记（SPEC-MT-2）。继承来的槽位绝不允许伪装成用户原文。
SLOT_VERBATIM = "verbatim"
SLOT_INHERITED = "inherited"


@dataclass(frozen=True)
class RetrievalRecord:
    """检索证据。

    刻意**不**并入 :class:`AuditRecord`：三个版本能对比的前提是它们吐出同一种审计记录，
    为了塞检索信息而改审计结构，等于把对比能力换掉（SPEC-GRAPH-1）。
    """

    query: str
    top_k: int
    hits: tuple[dict[str, Any], ...] = ()
    grounded: bool = False

    @property
    def doc_ids(self) -> tuple[str, ...]:
        return tuple(hit["doc_id"] for hit in self.hits)

    @property
    def top_doc_id(self) -> str | None:
        return None if not self.hits else self.hits[0]["doc_id"]

    @property
    def expired_doc_ids(self) -> tuple[str, ...]:
        return tuple(hit["doc_id"] for hit in self.hits if hit.get("expired"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "top_k": self.top_k,
            "hits": [dict(hit) for hit in self.hits],
            "grounded": self.grounded,
            "doc_ids": list(self.doc_ids),
            "top_doc_id": self.top_doc_id,
            "expired_doc_ids": list(self.expired_doc_ids),
        }


@dataclass(frozen=True)
class SlotRecord:
    """槽位来源。审计里只放值，来源放这里——两者混在一起就没法归因（SPEC-MT-2）。"""

    order_id: str | None = None
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"order_id": self.order_id, "source": self.source}


@dataclass(frozen=True)
class AuditRecord:
    """One complete, serializable decision and tool-execution record."""

    intent: Intent
    state: AgentState
    tool_name: str | None
    arguments: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, str | None] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentResponse:
    message: str
    intent: Intent
    state: AgentState
    audit: AuditRecord

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

