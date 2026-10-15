"""Shared fixtures and eval-case helpers for the AgentEvalOps test suite."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentevalops.agent import EcommerceSupportAgent
from agentevalops.order_store import OrderStore
from agentevalops.tools import HumanTicketTool, OrderStatusTool, RefundTool

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_CASES_PATH = REPO_ROOT / "eval_cases" / "support_cases.jsonl"

# Mirrors src/agentevalops/data/orders.json. Tests use this copy so they never depend on
# the packaged file being importable, while a dedicated test keeps both in sync.
MOCK_ORDERS: list[dict[str, Any]] = [
    {
        "order_id": "ORD-1001",
        "customer_id": "CUST-001",
        "status": "shipped",
        "tracking_number": "SF1234567890",
    },
    {
        "order_id": "ORD-1002",
        "customer_id": "CUST-001",
        "status": "delivered",
        "tracking_number": "YT9876543210",
    },
    {
        "order_id": "ORD-1003",
        "customer_id": "CUST-001",
        "status": "cancelled",
        "tracking_number": None,
    },
    {
        "order_id": "ORD-1004",
        "customer_id": "CUST-001",
        "status": "delivered",
        "tracking_number": "JD5566778899",
    },
    {
        "order_id": "ORD-1005",
        "customer_id": "CUST-001",
        "status": "processing",
        "tracking_number": None,
    },
    {
        "order_id": "ORD-2001",
        "customer_id": "CUST-002",
        "status": "processing",
        "tracking_number": None,
    },
    {
        "order_id": "ORD-2002",
        "customer_id": "CUST-002",
        "status": "delivered",
        "tracking_number": "ZTO1122334455",
    },
]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse a JSONL file, reporting the offending line number on failure."""
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise AssertionError(f"{path.name}:{line_number} 不是合法 JSON: {exc}") from exc
    return records


def load_support_cases() -> list[dict[str, Any]]:
    """Load the data-driven scenario table used by the evaluation tests."""
    return read_jsonl(EVAL_CASES_PATH)


@pytest.fixture
def orders() -> list[dict[str, Any]]:
    """A mutable copy of the mock orders, so individual tests can tamper freely."""
    return [dict(order) for order in MOCK_ORDERS]


@pytest.fixture
def store(orders: list[dict[str, Any]]) -> OrderStore:
    return OrderStore(orders)


@pytest.fixture
def status_tool(store: OrderStore) -> OrderStatusTool:
    return OrderStatusTool(store)


@pytest.fixture
def refund_tool(store: OrderStore) -> RefundTool:
    return RefundTool(store)


@pytest.fixture
def ticket_tool(store: OrderStore) -> HumanTicketTool:
    return HumanTicketTool(store)


@pytest.fixture
def agent(store: OrderStore) -> EcommerceSupportAgent:
    """A fresh agent per test, so tool counters always start from a known state."""
    return EcommerceSupportAgent(store=store)
