"""
Kroger / City Market API client.

Two auth contexts:
  - Client credentials (scope product.compact) — product search, store locator
  - Authorization code  (scope cart.basic:write) — cart writes, tied to the real account

Cart writes use the Public Cart API (PUT /v1/cart/add), NOT the Partner-tier "Carts"
collection API (POST /v1/carts), which this app isn't approved for. The cart is
write-only: there is no way to read or clear it through this API.

Docs: https://developer.kroger.com/api-products/api/overview
"""

import base64
import os
import time
from datetime import datetime, timedelta

import httpx

from db import session  # noqa: F401 — importing db also loads .env before the reads below
from models import KrogerToken, Profile

BASE = "https://api.kroger.com/v1"
AUTHORIZE_URL = f"{BASE}/connect/oauth2/authorize"
TOKEN_URL = f"{BASE}/connect/oauth2/token"

CART_SCOPE = "cart.basic:write"
PRODUCT_SCOPE = "product.compact"

REDIRECT_PORT = 8734
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"

CLIENT_ID = os.getenv("KROGER_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("KROGER_CLIENT_SECRET", "")
DEFAULT_ZIP = os.getenv("SHOPPER_ZIP", "81224")

# Lets an already-working refresh token be dropped in via .env, so the one-time
# browser OAuth in auth_cli.py can be skipped entirely.
SEED_REFRESH_TOKEN = os.getenv("KROGER_USER_REFRESH_TOKEN", "")

TIMEOUT = 20.0


class KrogerError(RuntimeError):
    pass


def basic_auth_header() -> str:
    raw = f"{CLIENT_ID}:{CLIENT_SECRET}".encode()
    return f"Basic {base64.b64encode(raw).decode()}"


def credentials_configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET and not CLIENT_ID.startswith("your_"))


# --------------------------------------------------------------------------
# Client-credentials token (search + locations, no user login)
# --------------------------------------------------------------------------

_client_token: dict | None = None


def get_client_token() -> str:
    global _client_token
    if _client_token and _client_token["expires_at"] > time.time():
        return _client_token["token"]

    if not credentials_configured():
        raise KrogerError("KROGER_CLIENT_ID / KROGER_CLIENT_SECRET are not set in .env")

    res = httpx.post(
        TOKEN_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": basic_auth_header(),
        },
        data={"grant_type": "client_credentials", "scope": PRODUCT_SCOPE},
        timeout=TIMEOUT,
    )
    if res.status_code != 200:
        raise KrogerError(f"client token error {res.status_code}: {res.text[:300]}")

    data = res.json()
    # 60s safety margin so a token never expires mid-request.
    _client_token = {"token": data["access_token"], "expires_at": time.time() + data["expires_in"] - 60}
    return _client_token["token"]


# --------------------------------------------------------------------------
# User token (cart writes)
# --------------------------------------------------------------------------


def exchange_code_for_token(code: str) -> None:
    res = httpx.post(
        TOKEN_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": basic_auth_header(),
        },
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI},
        timeout=TIMEOUT,
    )
    if res.status_code != 200:
        raise KrogerError(f"code exchange failed {res.status_code}: {res.text[:300]}")
    _store_token(res.json())


def _store_token(data: dict) -> None:
    with session() as s:
        row = s.get(KrogerToken, 1) or KrogerToken(id=1)
        row.access_token = data["access_token"]
        if data.get("refresh_token"):
            row.refresh_token = data["refresh_token"]
        row.expires_at = datetime.now() + timedelta(seconds=data.get("expires_in", 1800) - 60)
        s.add(row)
        s.commit()


_last_refresh_error: str | None = None


def _seed_refresh_token() -> None:
    """Adopt KROGER_USER_REFRESH_TOKEN from .env if nothing is stored yet."""
    if not SEED_REFRESH_TOKEN:
        return
    with session() as s:
        row = s.get(KrogerToken, 1) or KrogerToken(id=1)
        if row.refresh_token:
            return
        row.refresh_token = SEED_REFRESH_TOKEN.strip()
        s.add(row)
        s.commit()


def refresh_user_token() -> str | None:
    _seed_refresh_token()
    with session() as s:
        row = s.get(KrogerToken, 1)
        refresh = row.refresh_token if row else None

    if not refresh:
        return None

    res = httpx.post(
        TOKEN_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": basic_auth_header(),
        },
        data={"grant_type": "refresh_token", "refresh_token": refresh},
        timeout=TIMEOUT,
    )
    if res.status_code != 200:
        global _last_refresh_error
        _last_refresh_error = f"{res.status_code}: {res.text[:200]}"
        return None

    data = res.json()
    _store_token(data)
    return data["access_token"]


def get_user_token() -> str | None:
    with session() as s:
        row = s.get(KrogerToken, 1)
        if row and row.access_token and row.expires_at and row.expires_at > datetime.now():
            return row.access_token
    return refresh_user_token()


def is_connected() -> bool:
    _seed_refresh_token()
    with session() as s:
        row = s.get(KrogerToken, 1)
        return bool(row and row.refresh_token)


def authorize_url() -> str:
    params = httpx.QueryParams(
        {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": CART_SCOPE,
        }
    )
    return f"{AUTHORIZE_URL}?{params}"


# --------------------------------------------------------------------------
# Store locator
# --------------------------------------------------------------------------


def get_location_id() -> str | None:
    """Resolve and cache the nearest store id, preferring a City Market."""
    with session() as s:
        profile = s.get(Profile, 1)
        if profile and profile.location_id:
            return profile.location_id
        zip_code = (profile.zip_code if profile else None) or DEFAULT_ZIP

    token = get_client_token()
    res = httpx.get(
        f"{BASE}/locations",
        params={
            "filter.zipCode.near": zip_code,
            "filter.radiusInMiles": 50,
            "filter.limit": 5,
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )
    if res.status_code != 200:
        return None

    locations = res.json().get("data", [])
    if not locations:
        return None

    chosen = next(
        (loc for loc in locations if "city market" in f"{loc.get('name', '')} {loc.get('chain', '')}".lower()),
        locations[0],
    )
    location_id = chosen.get("locationId")
    if not location_id:
        return None

    with session() as s:
        profile = s.get(Profile, 1) or Profile(id=1, zip_code=zip_code)
        profile.location_id = location_id
        profile.store_name = chosen.get("name")
        s.add(profile)
        s.commit()

    return location_id


# --------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------


def _map_product(p: dict) -> dict:
    item = (p.get("items") or [{}])[0]
    return {
        "upc": p.get("upc"),
        "description": p.get("description"),
        "brand": p.get("brand"),
        "size": item.get("size"),
        "sold_by": item.get("soldBy"),
        "stock_level": (item.get("inventory") or {}).get("stockLevel"),
        "price": (item.get("price") or {}).get("regular"),
    }


def search_products(term: str, brand: str | None = None, limit: int = 10) -> list[dict]:
    """Free-text product search at the resolved store.

    fulfillment=csp (curbside pickup) matches the cart API's default PICKUP modality,
    so searched items are the same ones a pushed cart can actually fulfill.
    """
    location_id = get_location_id()
    if not location_id:
        return []

    params = {
        "filter.term": term,
        "filter.locationId": location_id,
        "filter.limit": min(limit, 50),
        "filter.fulfillment": "csp",
    }
    if brand:
        params["filter.brand"] = brand

    res = httpx.get(
        f"{BASE}/products",
        params=params,
        headers={"Authorization": f"Bearer {get_client_token()}"},
        timeout=TIMEOUT,
    )
    if res.status_code != 200:
        return []
    return [_map_product(p) for p in res.json().get("data", [])]


def pad_upc(upc: str) -> str:
    """Kroger expects 13-digit GTINs; receipt SKUs are often shorter."""
    return upc.strip().zfill(13)


def lookup_products_by_upc(upcs: list[str]) -> list[dict]:
    """Exact-match lookup, batched at the API's 50-id limit for filter.productId."""
    if not upcs:
        return []

    location_id = get_location_id()
    if not location_id:
        return []

    token = get_client_token()
    padded = [pad_upc(u) for u in upcs]
    results: list[dict] = []

    for i in range(0, len(padded), 50):
        res = httpx.get(
            f"{BASE}/products",
            params={
                "filter.productId": ",".join(padded[i : i + 50]),
                "filter.locationId": location_id,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
        if res.status_code != 200:
            continue
        results.extend(_map_product(p) for p in res.json().get("data", []))

    return results


# --------------------------------------------------------------------------
# Cart
# --------------------------------------------------------------------------


def add_to_cart(items: list[dict]) -> dict:
    """Add items to the user's cart in one batched PUT. Returns 204 on success.

    items: [{"upc": str, "quantity": int}]
    """
    if not items:
        return {"success": True, "added": 0}

    token = get_user_token()
    if not token:
        return {"success": False, "error": "Not connected to Kroger — run `python auth_cli.py`"}

    payload = {"items": [{"upc": pad_upc(i["upc"]), "quantity": int(i.get("quantity", 1))} for i in items]}

    def put(bearer: str) -> httpx.Response:
        return httpx.put(
            f"{BASE}/cart/add",
            json=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"},
            timeout=TIMEOUT,
        )

    res = put(token)
    if res.status_code == 401:
        refreshed = refresh_user_token()
        if refreshed:
            res = put(refreshed)

    if res.status_code in (200, 204):
        return {"success": True, "added": len(payload["items"])}
    return {"success": False, "error": f"{res.status_code}: {res.text[:300]}"}
