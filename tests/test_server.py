"""Tests for the MCP tool layer and the cadence math behind it."""

from datetime import date, timedelta

import pytest

import kroger
import server
import trends as trends_mod
from conftest import location_payload, product_payload
from db import session
from models import KnownItem

TODAY = date.today()


def _record_weekly(names, weeks=5, store="City Market"):
    """One receipt per week going back `weeks`, each containing every name."""
    for w in reversed(range(weeks)):
        server.record_receipt(
            store,
            (TODAY - timedelta(days=7 * w)).isoformat(),
            [{"name": n.title(), "normalized_name": n, "quantity": 1, "unit_price": 2.5} for n in names],
            total=2.5 * len(names),
        )


# --------------------------------------------------------------------------
# Context
# --------------------------------------------------------------------------


def test_context_created_on_first_read():
    context = server.get_shopping_context()
    assert context["zip_code"] == "81224"
    assert context["health_profile"] is None
    assert context["kroger_connected"] is False


def test_update_context_sets_only_given_fields():
    server.update_shopping_context(health_profile="low sodium")
    server.update_shopping_context(shopping_notes="budget $150/wk")

    context = server.get_shopping_context()
    assert context["health_profile"] == "low sodium"
    assert context["shopping_notes"] == "budget $150/wk"


def test_changing_zip_clears_cached_store():
    server.update_shopping_context(store_name="City Market")
    with session() as s:
        from models import Profile

        p = s.get(Profile, 1)
        p.location_id = "62000419"
        s.add(p)
        s.commit()

    server.update_shopping_context(zip_code="80202")
    assert server.get_shopping_context()["location_id"] is None


# --------------------------------------------------------------------------
# Receipts
# --------------------------------------------------------------------------


def test_record_receipt_returns_id_and_count():
    result = server.record_receipt(
        "City Market",
        TODAY.isoformat(),
        [{"name": "MILK 2%", "normalized_name": "milk", "quantity": 1, "unit_price": 3.99}],
        total=3.99,
    )
    assert result["items_recorded"] == 1
    assert isinstance(result["receipt_id"], int)


def test_record_receipt_normalizes_case_and_whitespace():
    server.record_receipt(
        "City Market", TODAY.isoformat(), [{"name": "MILK", "normalized_name": "  MiLk  ", "quantity": 1}]
    )
    assert server.get_receipt(1)["items"][0]["normalized_name"] == "milk"


def test_record_receipt_caches_upc_as_known_item():
    server.record_receipt(
        "City Market",
        TODAY.isoformat(),
        [{"name": "Bananas", "normalized_name": "bananas", "quantity": 1, "upc": "0000000004011"}],
    )
    with session() as s:
        known = s.get(KnownItem, "bananas")
    assert known.upc == "0000000004011"


def test_record_receipt_without_upc_creates_no_known_item():
    server.record_receipt("City Market", TODAY.isoformat(), [{"name": "Bulk Oats", "normalized_name": "oats", "quantity": 1}])
    with session() as s:
        assert s.get(KnownItem, "oats") is None


def test_known_item_upc_updated_on_rebuy():
    for upc in ["0000000000001", "0000000000002"]:
        server.record_receipt(
            "City Market", TODAY.isoformat(), [{"name": "Milk", "normalized_name": "milk", "quantity": 1, "upc": upc}]
        )
    with session() as s:
        assert s.get(KnownItem, "milk").upc == "0000000000002"


def test_list_receipts_is_newest_first_with_counts():
    _record_weekly(["milk", "eggs"], weeks=3)
    receipts = server.list_receipts(limit=2)

    assert len(receipts) == 2
    assert receipts[0]["purchased_on"] > receipts[1]["purchased_on"]
    assert receipts[0]["item_count"] == 2


def test_get_receipt_returns_line_items():
    server.record_receipt(
        "City Market",
        TODAY.isoformat(),
        [{"name": "Eggs", "normalized_name": "eggs", "quantity": 2, "unit_price": 4.5, "category": "dairy"}],
        total=9.0,
    )
    receipt = server.get_receipt(1)
    assert receipt["total"] == 9.0
    assert receipt["items"][0] == {
        "name": "Eggs",
        "normalized_name": "eggs",
        "quantity": 2.0,
        "unit_price": 4.5,
        "upc": None,
        "category": "dairy",
    }


def test_get_receipt_missing_id():
    assert "error" in server.get_receipt(999)


# --------------------------------------------------------------------------
# Trends
# --------------------------------------------------------------------------


def test_weekly_item_cadence():
    _record_weekly(["milk"], weeks=5)
    [milk] = server.get_purchase_trends()

    assert milk["times_bought"] == 5
    assert milk["avg_days_between"] == 7.0


def test_single_purchase_excluded_by_min_purchases():
    server.record_receipt("City Market", TODAY.isoformat(), [{"name": "Cake", "normalized_name": "cake", "quantity": 1}])
    assert server.get_purchase_trends(min_purchases=2) == []


def test_erratic_item_has_high_gap_variability():
    for days_ago in [200, 40, 39, 2]:
        server.record_receipt(
            "City Market",
            (TODAY - timedelta(days=days_ago)).isoformat(),
            [{"name": "Capers", "normalized_name": "capers", "quantity": 1}],
        )
    [capers] = server.get_purchase_trends()
    assert capers["times_bought"] == 4
    assert capers["gap_variability"] > 0.75


def test_overdue_computed_against_average_gap():
    # Weekly item, last bought 14 days ago -> ~7 days overdue.
    for w in [4, 3, 2]:
        server.record_receipt(
            "City Market",
            (TODAY - timedelta(days=7 * w)).isoformat(),
            [{"name": "Milk", "normalized_name": "milk", "quantity": 1}],
        )
    [milk] = server.get_purchase_trends()
    assert milk["days_since_last"] == 14
    assert milk["overdue_by"] == pytest.approx(7.0)


def test_trends_sorted_most_overdue_first():
    _record_weekly(["milk"], weeks=5)
    for w in [5, 4, 3]:  # stopped buying eggs three weeks ago
        server.record_receipt(
            "City Market",
            (TODAY - timedelta(days=7 * w)).isoformat(),
            [{"name": "Eggs", "normalized_name": "eggs", "quantity": 1}],
        )
    assert [t["normalized_name"] for t in server.get_purchase_trends()][0] == "eggs"


def test_trip_cadence_needs_two_trips():
    server.record_receipt("City Market", TODAY.isoformat(), [{"name": "Milk", "normalized_name": "milk", "quantity": 1}])
    assert trends_mod.trip_cadence()["median_trip_interval_days"] is None


def test_trip_cadence_median_interval():
    _record_weekly(["milk"], weeks=4)
    cadence = trends_mod.trip_cadence()
    assert cadence["receipts"] == 4
    assert cadence["median_trip_interval_days"] == 7.0
    assert cadence["days_since_last_trip"] == 0


# --------------------------------------------------------------------------
# Kroger-backed tools
# --------------------------------------------------------------------------


@pytest.mark.respx(base_url=kroger.BASE)
def test_kroger_status_reports_disconnected_with_fix(respx_mock):
    status = server.kroger_status()
    assert status["connected"] is False
    assert "auth_cli" in status["fix"]


@pytest.mark.respx(base_url=kroger.BASE)
def test_kroger_status_connected(respx_mock):
    respx_mock.post(kroger.TOKEN_URL).respond(200, json={"access_token": "acc", "expires_in": 1800})
    with session() as s:
        from models import KrogerToken

        s.add(KrogerToken(id=1, refresh_token="ref"))
        s.commit()

    assert server.kroger_status()["connected"] is True


@pytest.mark.respx(base_url=kroger.BASE)
def test_search_products_tool_passes_through(respx_mock, token_route):
    respx_mock.get(f"{kroger.BASE}/locations").respond(200, json={"data": [location_payload()]})
    respx_mock.get(f"{kroger.BASE}/products").respond(200, json={"data": [product_payload()]})

    [item] = server.search_products("bananas", limit=1)
    assert item["upc"] == "0000000004011"


def test_resolve_known_upcs_splits_hits_and_misses():
    server.record_receipt(
        "City Market",
        TODAY.isoformat(),
        [{"name": "Bananas", "normalized_name": "bananas", "quantity": 1, "upc": "0000000004011"}],
    )
    result = server.resolve_known_upcs(["Bananas", "quinoa"])

    assert result["resolved"]["Bananas"]["upc"] == "0000000004011"
    assert result["unresolved"] == ["quinoa"]


@pytest.mark.respx(base_url=kroger.BASE)
def test_add_to_cart_tool_reports_failure_without_connection(respx_mock):
    assert server.add_to_cart([{"upc": "4011", "quantity": 1}])["success"] is False
