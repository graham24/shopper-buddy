"""Tests for the cart push log."""

import planner
import server
from db import session
from models import CartPush, CartPushItem


def test_log_push_records_items_and_note():
    push_id = planner.log_push(
        [{"upc": "111", "quantity": 2, "name": "KRO MILK", "normalized_name": "Milk"}], note="weekly fill"
    )
    with session() as s:
        push = s.get(CartPush, push_id)
        [item] = s.exec(CartPushItem.__table__.select()).all()

    assert push.item_count == 1 and push.note == "weekly fill"
    assert item.normalized_name == "milk"  # normalized on the way in
    assert item.quantity == 2.0


def test_get_cart_history_newest_first():
    planner.log_push([{"upc": "1", "quantity": 1}], note="first")
    planner.log_push([{"upc": "2", "quantity": 1}], note="second")

    history = server.get_cart_history(limit=2)
    assert [h["note"] for h in history] == ["second", "first"]


def test_cart_history_empty_initially():
    assert server.get_cart_history() == []


def test_add_to_cart_logs_only_on_success(respx_mock, monkeypatch):
    monkeypatch.setattr(server.kroger, "add_to_cart", lambda items: {"success": False, "error": "boom"})
    server.add_to_cart([{"upc": "111", "quantity": 1}])
    assert server.get_cart_history() == []


def test_add_to_cart_logs_push_on_success(respx_mock, monkeypatch):
    monkeypatch.setattr(server.kroger, "add_to_cart", lambda items: {"success": True, "added": len(items)})

    result = server.add_to_cart([{"upc": "111", "quantity": 1, "normalized_name": "milk"}], note="weekly")

    assert "push_id" in result
    assert server.get_cart_history()[0]["items"][0]["normalized_name"] == "milk"
