"""Local JSON-backed order repository."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any


class OrderStore:
    def __init__(self, orders: list[dict[str, Any]] | None = None) -> None:
        if orders is None:
            data_file = files("agentevalops.data").joinpath("orders.json")
            orders = json.loads(data_file.read_text(encoding="utf-8"))
        self._orders = {order["order_id"]: dict(order) for order in orders}

    def get(self, order_id: str) -> dict[str, Any] | None:
        order = self._orders.get(order_id)
        return dict(order) if order else None

    def get_owned(self, order_id: str, customer_id: str) -> dict[str, Any] | None:
        order = self.get(order_id)
        if order is None or order["customer_id"] != customer_id:
            return None
        return order

