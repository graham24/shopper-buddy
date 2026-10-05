#!/usr/bin/env python
"""Shopper Buddy — MCP server for AI-assisted grocery shopping.

The server holds no intelligence: it stores purchase history, computes cadence,
and talks to the Kroger API. All parsing, matching, and health-based judgment
happens in the conversation.
"""

import os
from datetime import date

from mcp.server.mcpserver import MCPServer
from sqlmodel import desc, select

import kroger
import planner
import trends as trends_mod
from db import init_db, session
from models import KnownItem, Profile, Receipt, ReceiptItem

mcp = MCPServer(
    "shopper-buddy",
    instructions=(
        "Grocery shopping assistant backed by the user's real purchase history and "
        "Kroger account.\n\n"
        "For 'fill my cart for the week' or any restock request: call get_purchase_trends "
        "and get_cart_history, decide yourself what is due, resolve_known_upcs for those "
        "items, and search_products only for what's left, choosing against the "
        "health_profile. Show the full list — names, quantities, prices, total — and call "
        "add_to_cart only after the user confirms.\n\n"
        "When the user shares a receipt, parse it yourself and call record_receipt. "
        "Reuse the existing normalized_name from get_purchase_trends for items bought "
        "before, or trends will split across two names."
    ),
)


def _profile(s) -> Profile:
    profile = s.get(Profile, 1)
    if not profile:
        profile = Profile(id=1, zip_code=kroger.DEFAULT_ZIP)
        s.add(profile)
        s.commit()
        s.refresh(profile)
    return profile


# --------------------------------------------------------------------------
# Context
# --------------------------------------------------------------------------


@mcp.tool()
def get_shopping_context() -> dict:
    """Household shopping context: health profile, preferences, store, trip cadence.

    Call this FIRST in any shopping conversation. The health_profile and
    shopping_notes are freeform instructions from the user about how to shop for
    them — treat them as binding when choosing between products.
    """
    with session() as s:
        p = _profile(s)
        context = {
            "zip_code": p.zip_code,
            "store_name": p.store_name,
            "location_id": p.location_id,
            "health_profile": p.health_profile,
            "shopping_notes": p.shopping_notes,
        }
    context["kroger_connected"] = kroger.is_connected()
    context["trip_cadence"] = trends_mod.trip_cadence()
    return context


@mcp.tool()
def update_shopping_context(
    health_profile: str | None = None,
    shopping_notes: str | None = None,
    zip_code: str | None = None,
    store_name: str | None = None,
) -> dict:
    """Update the household context. Only the fields passed are changed.

    health_profile: freeform dietary guidance ("low sodium, high protein, no dairy").
    shopping_notes: freeform preferences ("budget ~$150/week, prefer Simple Truth").
    Changing zip_code clears the cached store so it re-resolves on the next search.
    """
    with session() as s:
        p = _profile(s)
        if health_profile is not None:
            p.health_profile = health_profile
        if shopping_notes is not None:
            p.shopping_notes = shopping_notes
        if store_name is not None:
            p.store_name = store_name
        if zip_code is not None and zip_code != p.zip_code:
            p.zip_code = zip_code
            p.location_id = None
        s.add(p)
        s.commit()
    return get_shopping_context()


# --------------------------------------------------------------------------
# Receipts
# --------------------------------------------------------------------------


@mcp.tool()
def record_receipt(store: str, purchased_on: str, items: list[dict], total: float | None = None) -> dict:
    """Store a parsed receipt. You parse the receipt image or text, then call this.

    purchased_on: ISO date (YYYY-MM-DD).
    items: [{name, normalized_name, quantity, unit_price?, upc?, category?}]

    normalized_name is the join key for trend detection, so it must be stable
    across trips: lowercase, singular, no brand/size/packaging
    ("KRO 2% MILK GAL" and "Kroger Milk 2% 1gal" both -> "milk"). Reuse the exact
    normalized_name from get_purchase_trends when an item has been bought before.
    Items with a upc are cached so future trips skip product search.
    """
    with session() as s:
        receipt = Receipt(store=store, purchased_on=date.fromisoformat(purchased_on), total=total)
        s.add(receipt)
        s.commit()
        s.refresh(receipt)

        for item in items:
            normalized = item["normalized_name"].strip().lower()
            s.add(
                ReceiptItem(
                    receipt_id=receipt.id,
                    name=item["name"],
                    normalized_name=normalized,
                    quantity=float(item.get("quantity", 1)),
                    unit_price=item.get("unit_price"),
                    upc=item.get("upc"),
                    category=item.get("category"),
                )
            )

            if item.get("upc"):
                known = s.get(KnownItem, normalized) or KnownItem(normalized_name=normalized, upc=item["upc"])
                known.upc = item["upc"]
                known.description = item.get("name")
                known.last_seen = receipt.purchased_on
                s.add(known)

        s.commit()
        return {"receipt_id": receipt.id, "items_recorded": len(items)}


@mcp.tool()
def list_receipts(limit: int = 10) -> list[dict]:
    """Recent receipts, newest first, with item counts."""
    with session() as s:
        receipts = s.exec(select(Receipt).order_by(desc(Receipt.purchased_on)).limit(limit)).all()
        return [
            {
                "receipt_id": r.id,
                "store": r.store,
                "purchased_on": r.purchased_on.isoformat(),
                "total": r.total,
                "item_count": len(s.exec(select(ReceiptItem).where(ReceiptItem.receipt_id == r.id)).all()),
            }
            for r in receipts
        ]


@mcp.tool()
def get_receipt(receipt_id: int) -> dict:
    """Full line items for one receipt."""
    with session() as s:
        r = s.get(Receipt, receipt_id)
        if not r:
            return {"error": f"No receipt {receipt_id}"}
        items = s.exec(select(ReceiptItem).where(ReceiptItem.receipt_id == receipt_id)).all()
        return {
            "receipt_id": r.id,
            "store": r.store,
            "purchased_on": r.purchased_on.isoformat(),
            "total": r.total,
            "items": [
                {
                    "name": i.name,
                    "normalized_name": i.normalized_name,
                    "quantity": i.quantity,
                    "unit_price": i.unit_price,
                    "upc": i.upc,
                    "category": i.category,
                }
                for i in items
            ],
        }


# --------------------------------------------------------------------------
# Trends
# --------------------------------------------------------------------------


@mcp.tool()
def get_purchase_trends(min_purchases: int = 2) -> list[dict]:
    """Per-item buying pattern across all receipts, most overdue first.

    This is raw cadence — the server does not decide what's a staple or what's due.
    Judge that yourself: an item bought many times with low gap_variability and
    days_until_due inside the trip horizon is a restock; few purchases or erratic
    gaps is an occasional buy to ask about, not add. Check get_cart_history too, so
    something pushed a few days ago isn't ordered twice.

    Also use this to look up the existing normalized_name for an item before
    recording a new receipt.
    """
    return trends_mod.purchase_trends(min_purchases=min_purchases)


# --------------------------------------------------------------------------
# Kroger
# --------------------------------------------------------------------------


@mcp.tool()
def kroger_status() -> dict:
    """Whether the Kroger account is connected and which store is resolved."""
    # Verify against Kroger rather than just checking for a stored token — refresh
    # tokens rotate on use, so a stored one can be silently dead.
    connected = bool(kroger.get_user_token())
    status = {
        "credentials_configured": kroger.credentials_configured(),
        "connected": connected,
    }
    if not connected:
        status["reason"] = kroger._last_refresh_error or "no refresh token stored"
        status["fix"] = "Run `venv/bin/python auth_cli.py` in the shopper-buddy directory to connect."
    with session() as s:
        p = _profile(s)
        status["store_name"] = p.store_name
        status["location_id"] = p.location_id
    return status


@mcp.tool()
def search_products(term: str, brand: str | None = None, limit: int = 10) -> list[dict]:
    """Search the store's catalog. Returns upc, description, brand, size, price, stock.

    Pick among the results using the user's health_profile — the server does no
    filtering of its own. Search only for items resolve_known_upcs didn't cover.
    """
    return kroger.search_products(term, brand=brand, limit=limit)


@mcp.tool()
def resolve_known_upcs(normalized_names: list[str]) -> dict:
    """Look up UPCs the household has bought before, by normalized_name.

    Call this before search_products — anything it resolves is an item the user
    has actually bought, so it needs no search and no re-picking.
    """
    with session() as s:
        found = {}
        for name in normalized_names:
            known = s.get(KnownItem, name.strip().lower())
            if known:
                found[name] = {
                    "upc": known.upc,
                    "description": known.description,
                    "brand": known.brand,
                    "size": known.size,
                    "last_seen": known.last_seen.isoformat(),
                }
    return {"resolved": found, "unresolved": [n for n in normalized_names if n not in found]}


@mcp.tool()
def add_to_cart(items: list[dict], note: str | None = None) -> dict:
    """Add items to the user's real Kroger cart.

    items: [{upc, quantity, name?, normalized_name?}] — pass name and normalized_name
    whenever you have them so the push log is readable and future orders can
    tell what has already been ordered.

    This leaves the machine and changes the user's actual cart, and the Kroger API
    offers no way to read or undo it. Always show the full list — item names,
    quantities, and prices — and get explicit confirmation before calling this.
    """
    result = kroger.add_to_cart(items)
    if result.get("success") and items:
        result["push_id"] = planner.log_push(items, note=note)
    return result


@mcp.tool()
def get_cart_history(limit: int = 5) -> list[dict]:
    """What was pushed to the cart before, newest first.

    Kroger can't tell us what's in the cart, so this log is the only record of what
    was ordered. Use it to answer "what did I order last week?".
    """
    return planner.last_push(limit)


if __name__ == "__main__":
    init_db()
    # stdio by default (how Claude Code launches us). Set SHOPPER_HTTP=1 to serve
    # streamable-http instead, for clients that can't spawn a subprocess — the
    # fitness app's chat talks to us that way.
    if os.getenv("SHOPPER_HTTP"):
        mcp.run(
            transport="streamable-http",
            host=os.getenv("SHOPPER_HOST", "127.0.0.1"),
            port=int(os.getenv("SHOPPER_PORT", "8765")),
        )
    else:
        mcp.run()
