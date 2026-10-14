"""Unit tests for the local order repository."""

from __future__ import annotations

from agentevalops.order_store import OrderStore


def test_loads_the_packaged_orders_by_default():
    store = OrderStore()
    assert store.get("ORD-1001")["status"] == "shipped"


def test_packaged_orders_match_the_test_fixture(orders):
    store = OrderStore()
    for order in orders:
        assert store.get(order["order_id"]) == order


def test_get_returns_none_for_an_unknown_order(store):
    assert store.get("ORD-0000") is None


def test_get_returns_a_defensive_copy(store):
    first_read = store.get("ORD-1001")
    first_read["status"] = "tampered"
    assert store.get("ORD-1001")["status"] == "shipped"


def test_constructor_does_not_alias_the_input_list(orders):
    store = OrderStore(orders)
    orders[0]["status"] = "tampered"
    assert store.get("ORD-1001")["status"] == "shipped"


def test_get_owned_returns_the_order_for_its_owner(store):
    assert store.get_owned("ORD-1001", "CUST-001")["order_id"] == "ORD-1001"


def test_get_owned_returns_none_for_another_customer(store):
    assert store.get_owned("ORD-2001", "CUST-001") is None


def test_get_owned_returns_none_for_an_unknown_order(store):
    assert store.get_owned("ORD-0000", "CUST-001") is None
