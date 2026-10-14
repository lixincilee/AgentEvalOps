"""Shared data contracts for tools, agent responses, and audit records."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Intent(StrEnum):
    ORDER_STATUS = "order_status"
    REFUND = "refund"
    HUMAN_HANDOFF = "human_handoff"
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

