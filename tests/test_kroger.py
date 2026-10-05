"""Tests for every Kroger API call, with httpx mocked at the transport layer."""

import time
from datetime import datetime, timedelta

import httpx
import pytest

import kroger
from conftest import location_payload, product_payload
from db import session
from models import KrogerToken, Profile

pytestmark = pytest.mark.respx(base_url=kroger.BASE)


# --------------------------------------------------------------------------
# Client-credentials token
# --------------------------------------------------------------------------


def test_client_token_requests_product_scope(respx_mock, token_route):
    assert kroger.get_client_token() == "client-tok"

    request = token_route.calls.last.request
    body = request.content.decode()
    assert "grant_type=client_credentials" in body
    assert "product.compact" in body
    assert request.headers["Authorization"].startswith("Basic ")


def test_client_token_is_cached_across_calls(respx_mock, token_route):
    kroger.get_client_token()
    kroger.get_client_token()
    assert token_route.call_count == 1


def test_client_token_refetched_once_expired(respx_mock, token_route):
    kroger.get_client_token()
    kroger._client_token["expires_at"] = time.time() - 1
    kroger.get_client_token()
    assert token_route.call_count == 2


def test_client_token_error_raises(respx_mock):
    respx_mock.post(kroger.TOKEN_URL).respond(401, text="nope")
    with pytest.raises(kroger.KrogerError):
        kroger.get_client_token()


def test_missing_credentials_raises(respx_mock, monkeypatch):
    monkeypatch.setattr(kroger, "CLIENT_ID", "")
    assert not kroger.credentials_configured()
    with pytest.raises(kroger.KrogerError):
        kroger.get_client_token()


# --------------------------------------------------------------------------
# User token / OAuth
# --------------------------------------------------------------------------


def test_exchange_code_stores_tokens(respx_mock):
    respx_mock.post(kroger.TOKEN_URL).respond(
        200, json={"access_token": "acc", "refresh_token": "ref", "expires_in": 1800}
    )
    kroger.exchange_code_for_token("the-code")

    with session() as s:
        row = s.get(KrogerToken, 1)
    assert (row.access_token, row.refresh_token) == ("acc", "ref")
    assert row.expires_at > datetime.now()


def test_exchange_code_sends_redirect_uri(respx_mock):
    route = respx_mock.post(kroger.TOKEN_URL).respond(200, json={"access_token": "a", "expires_in": 60})
    kroger.exchange_code_for_token("the-code")
    body = route.calls.last.request.content.decode()
    assert "grant_type=authorization_code" in body
    assert "code=the-code" in body
    assert "localhost%3A8734%2Fcallback" in body


def test_exchange_code_failure_raises(respx_mock):
    respx_mock.post(kroger.TOKEN_URL).respond(400, text="bad code")
    with pytest.raises(kroger.KrogerError):
        kroger.exchange_code_for_token("stale")


def test_refresh_rotates_stored_refresh_token(respx_mock):
    _store_refresh("old-ref")
    respx_mock.post(kroger.TOKEN_URL).respond(
        200, json={"access_token": "acc2", "refresh_token": "new-ref", "expires_in": 1800}
    )

    assert kroger.refresh_user_token() == "acc2"
    with session() as s:
        assert s.get(KrogerToken, 1).refresh_token == "new-ref"


def test_refresh_without_stored_token_returns_none(respx_mock):
    assert kroger.refresh_user_token() is None


def test_refresh_failure_records_reason(respx_mock):
    _store_refresh("dead-ref")
    respx_mock.post(kroger.TOKEN_URL).respond(400, json={"error": "invalid_request"})

    assert kroger.refresh_user_token() is None
    assert "400" in kroger._last_refresh_error


def test_get_user_token_uses_unexpired_stored_token(respx_mock):
    with session() as s:
        s.add(KrogerToken(id=1, access_token="live", refresh_token="r", expires_at=datetime.now() + timedelta(hours=1)))
        s.commit()

    # No token route registered — a network call here would fail the test.
    assert kroger.get_user_token() == "live"


def test_get_user_token_refreshes_when_expired(respx_mock):
    with session() as s:
        s.add(KrogerToken(id=1, access_token="stale", refresh_token="r", expires_at=datetime.now() - timedelta(minutes=1)))
        s.commit()
    respx_mock.post(kroger.TOKEN_URL).respond(200, json={"access_token": "fresh", "expires_in": 1800})

    assert kroger.get_user_token() == "fresh"


def test_seed_refresh_token_adopted_from_env(respx_mock):
    kroger.SEED_REFRESH_TOKEN = "seeded-ref"
    assert kroger.is_connected()
    with session() as s:
        assert s.get(KrogerToken, 1).refresh_token == "seeded-ref"


def test_seed_does_not_overwrite_existing_token(respx_mock):
    _store_refresh("real-ref")
    kroger.SEED_REFRESH_TOKEN = "seeded-ref"
    kroger.is_connected()
    with session() as s:
        assert s.get(KrogerToken, 1).refresh_token == "real-ref"


def test_is_connected_false_without_token(respx_mock):
    assert not kroger.is_connected()


def test_authorize_url_contains_cart_scope():
    url = kroger.authorize_url()
    assert "response_type=code" in url
    assert "cart.basic" in url
    assert "8734" in url


# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------


def test_location_prefers_city_market(respx_mock, token_route):
    respx_mock.get(f"{kroger.BASE}/locations").respond(
        200,
        json={"data": [
            {"locationId": "111", "name": "Kroger Somewhere", "chain": "KROGER"},
            location_payload("62000419"),
        ]},
    )
    assert kroger.get_location_id() == "62000419"


def test_location_falls_back_to_first_result(respx_mock, token_route):
    respx_mock.get(f"{kroger.BASE}/locations").respond(
        200, json={"data": [{"locationId": "111", "name": "Kroger Somewhere", "chain": "KROGER"}]}
    )
    assert kroger.get_location_id() == "111"


def test_location_cached_on_profile(respx_mock, token_route):
    route = respx_mock.get(f"{kroger.BASE}/locations").respond(200, json={"data": [location_payload()]})

    kroger.get_location_id()
    kroger.get_location_id()

    assert route.call_count == 1
    with session() as s:
        p = s.get(Profile, 1)
    assert (p.location_id, p.store_name) == ("62000419", "City Market - Gunnison")


def test_location_queries_profile_zip(respx_mock, token_route):
    with session() as s:
        s.add(Profile(id=1, zip_code="80202"))
        s.commit()
    route = respx_mock.get(f"{kroger.BASE}/locations").respond(200, json={"data": [location_payload()]})

    kroger.get_location_id()
    assert route.calls.last.request.url.params["filter.zipCode.near"] == "80202"


def test_location_returns_none_on_error(respx_mock, token_route):
    respx_mock.get(f"{kroger.BASE}/locations").respond(500)
    assert kroger.get_location_id() is None


def test_location_returns_none_when_empty(respx_mock, token_route):
    respx_mock.get(f"{kroger.BASE}/locations").respond(200, json={"data": []})
    assert kroger.get_location_id() is None


# --------------------------------------------------------------------------
# Product search
# --------------------------------------------------------------------------


def _stub_location(respx_mock):
    respx_mock.get(f"{kroger.BASE}/locations").respond(200, json={"data": [location_payload()]})


def test_search_products_maps_fields(respx_mock, token_route):
    _stub_location(respx_mock)
    respx_mock.get(f"{kroger.BASE}/products").respond(200, json={"data": [product_payload()]})

    [item] = kroger.search_products("bananas")
    assert item == {
        "upc": "0000000004011",
        "description": "Bananas",
        "brand": "Fresh",
        "size": "1 lb",
        "sold_by": "WEIGHT",
        "stock_level": "HIGH",
        "price": 0.69,
    }


def test_search_products_sends_filters(respx_mock, token_route):
    _stub_location(respx_mock)
    route = respx_mock.get(f"{kroger.BASE}/products").respond(200, json={"data": []})

    kroger.search_products("milk", brand="Simple Truth", limit=3)

    params = route.calls.last.request.url.params
    assert params["filter.term"] == "milk"
    assert params["filter.locationId"] == "62000419"
    assert params["filter.brand"] == "Simple Truth"
    assert params["filter.limit"] == "3"
    assert params["filter.fulfillment"] == "csp"


def test_search_products_caps_limit_at_api_max(respx_mock, token_route):
    _stub_location(respx_mock)
    route = respx_mock.get(f"{kroger.BASE}/products").respond(200, json={"data": []})
    kroger.search_products("milk", limit=500)
    assert route.calls.last.request.url.params["filter.limit"] == "50"


def test_search_products_handles_missing_item_details(respx_mock, token_route):
    _stub_location(respx_mock)
    respx_mock.get(f"{kroger.BASE}/products").respond(
        200, json={"data": [{"upc": "123", "description": "Mystery", "items": []}]}
    )
    [item] = kroger.search_products("mystery")
    assert item["price"] is None and item["size"] is None


def test_search_products_empty_without_location(respx_mock, token_route):
    respx_mock.get(f"{kroger.BASE}/locations").respond(200, json={"data": []})
    assert kroger.search_products("bananas") == []


def test_search_products_empty_on_error(respx_mock, token_route):
    _stub_location(respx_mock)
    respx_mock.get(f"{kroger.BASE}/products").respond(500)
    assert kroger.search_products("bananas") == []


# --------------------------------------------------------------------------
# UPC lookup
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw,padded", [("4011", "0000000004011"), (" 123 ", "0000000000123"), ("0000000004011", "0000000004011")])
def test_pad_upc(raw, padded):
    assert kroger.pad_upc(raw) == padded


def test_lookup_by_upc_pads_and_sends_product_ids(respx_mock, token_route):
    _stub_location(respx_mock)
    route = respx_mock.get(f"{kroger.BASE}/products").respond(200, json={"data": [product_payload()]})

    result = kroger.lookup_products_by_upc(["4011"])

    assert result[0]["description"] == "Bananas"
    assert route.calls.last.request.url.params["filter.productId"] == "0000000004011"


def test_lookup_by_upc_batches_at_fifty(respx_mock, token_route):
    _stub_location(respx_mock)
    route = respx_mock.get(f"{kroger.BASE}/products").respond(200, json={"data": []})

    kroger.lookup_products_by_upc([str(i) for i in range(120)])

    assert route.call_count == 3
    batches = [len(c.request.url.params["filter.productId"].split(",")) for c in route.calls]
    assert batches == [50, 50, 20]


def test_lookup_by_upc_skips_failed_batches(respx_mock, token_route):
    _stub_location(respx_mock)
    respx_mock.get(f"{kroger.BASE}/products").mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json={"data": [product_payload()]})]
    )
    assert len(kroger.lookup_products_by_upc([str(i) for i in range(60)])) == 1


def test_lookup_by_upc_empty_input_skips_network(respx_mock):
    assert kroger.lookup_products_by_upc([]) == []


# --------------------------------------------------------------------------
# Cart
# --------------------------------------------------------------------------


def _store_refresh(token="ref"):
    with session() as s:
        s.add(KrogerToken(id=1, refresh_token=token))
        s.commit()


def _store_live_access(token="live"):
    with session() as s:
        s.add(KrogerToken(id=1, access_token=token, refresh_token="ref", expires_at=datetime.now() + timedelta(hours=1)))
        s.commit()


def test_add_to_cart_success(respx_mock):
    _store_live_access()
    route = respx_mock.put(f"{kroger.BASE}/cart/add").respond(204)

    assert kroger.add_to_cart([{"upc": "4011", "quantity": 2}]) == {"success": True, "added": 1}

    payload = route.calls.last.request.read()
    assert b'"upc": "0000000004011"' in payload.replace(b'"upc":"', b'"upc": "')
    assert route.calls.last.request.headers["Authorization"] == "Bearer live"


def test_add_to_cart_batches_all_items_in_one_call(respx_mock):
    _store_live_access()
    route = respx_mock.put(f"{kroger.BASE}/cart/add").respond(204)

    result = kroger.add_to_cart([{"upc": "1", "quantity": 1}, {"upc": "2", "quantity": 3}])

    assert route.call_count == 1
    assert result["added"] == 2


def test_add_to_cart_defaults_quantity_to_one(respx_mock):
    _store_live_access()
    route = respx_mock.put(f"{kroger.BASE}/cart/add").respond(204)
    kroger.add_to_cart([{"upc": "4011"}])
    assert b'"quantity": 1' in route.calls.last.request.read().replace(b'"quantity":', b'"quantity": ')


def test_add_to_cart_retries_once_after_401(respx_mock):
    _store_live_access()
    respx_mock.post(kroger.TOKEN_URL).respond(200, json={"access_token": "refreshed", "expires_in": 1800})
    cart = respx_mock.put(f"{kroger.BASE}/cart/add").mock(
        side_effect=[httpx.Response(401), httpx.Response(204)]
    )

    assert kroger.add_to_cart([{"upc": "4011", "quantity": 1}])["success"] is True
    assert cart.call_count == 2
    assert cart.calls[1].request.headers["Authorization"] == "Bearer refreshed"


def test_add_to_cart_gives_up_when_refresh_fails_after_401(respx_mock):
    _store_live_access()
    respx_mock.post(kroger.TOKEN_URL).respond(400, json={"error": "invalid_request"})
    respx_mock.put(f"{kroger.BASE}/cart/add").respond(401)

    result = kroger.add_to_cart([{"upc": "4011", "quantity": 1}])
    assert result["success"] is False
    assert "401" in result["error"]


def test_add_to_cart_reports_server_error(respx_mock):
    _store_live_access()
    respx_mock.put(f"{kroger.BASE}/cart/add").respond(500, text="boom")

    result = kroger.add_to_cart([{"upc": "4011", "quantity": 1}])
    assert result["success"] is False
    assert "500" in result["error"]


def test_add_to_cart_without_connection(respx_mock):
    result = kroger.add_to_cart([{"upc": "4011", "quantity": 1}])
    assert result["success"] is False
    assert "auth_cli" in result["error"]


def test_add_to_cart_empty_skips_network(respx_mock):
    assert kroger.add_to_cart([]) == {"success": True, "added": 0}
