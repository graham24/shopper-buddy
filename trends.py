"""Cadence arithmetic over receipt history — how often things get rebought."""

import statistics
from datetime import date

from sqlmodel import select

from db import session
from models import Receipt, ReceiptItem


def _item_history() -> dict[str, list[tuple[date, float, float | None]]]:
    with session() as s:
        rows = s.exec(
            select(
                ReceiptItem.normalized_name,
                Receipt.purchased_on,
                ReceiptItem.quantity,
                ReceiptItem.unit_price,
            )
            .join(Receipt, Receipt.id == ReceiptItem.receipt_id)
            .order_by(Receipt.purchased_on)
        ).all()

    history: dict[str, list[tuple[date, float, float | None]]] = {}
    for name, purchased_on, quantity, unit_price in rows:
        history.setdefault(name, []).append((purchased_on, quantity, unit_price))
    return history


def purchase_trends(min_purchases: int = 2, as_of: date | None = None) -> list[dict]:
    today = as_of or date.today()
    trends = []

    for name, entries in _item_history().items():
        dates = sorted({d for d, _, _ in entries})
        if len(entries) < min_purchases:
            continue

        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        avg_gap = statistics.mean(gaps) if gaps else None
        variability = (statistics.stdev(gaps) / avg_gap) if len(gaps) > 1 and avg_gap else 0.0

        last_bought = dates[-1]
        days_since = (today - last_bought).days
        avg_quantity = statistics.mean(q for _, q, _ in entries)
        prices = [p for _, _, p in entries if p is not None]

        trends.append(
            {
                "normalized_name": name,
                "times_bought": len(entries),
                "first_bought": dates[0].isoformat(),
                "last_bought": last_bought.isoformat(),
                "avg_quantity": round(avg_quantity, 2),
                "suggested_quantity": max(1, round(avg_quantity)),
                "typical_price": round(statistics.median(prices), 2) if prices else None,
                "avg_days_between": round(avg_gap, 1) if avg_gap else None,
                "gap_variability": round(variability, 2),
                "days_since_last": days_since,
                "overdue_by": round(days_since - avg_gap, 1) if avg_gap else None,
                "days_until_due": round(avg_gap - days_since, 1) if avg_gap else None,
            }
        )

    trends.sort(key=lambda t: (t["overdue_by"] is None, -(t["overdue_by"] or 0)))
    return trends


def trip_cadence() -> dict:
    with session() as s:
        dates = sorted({r.purchased_on for r in s.exec(select(Receipt)).all()})

    if len(dates) < 2:
        return {"receipts": len(dates), "median_trip_interval_days": None, "last_trip": None}

    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    return {
        "receipts": len(dates),
        "median_trip_interval_days": round(statistics.median(gaps), 1),
        "last_trip": dates[-1].isoformat(),
        "days_since_last_trip": (date.today() - dates[-1]).days,
    }
