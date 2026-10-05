"""The cart push log — the only record of what was sent to the Kroger cart.

Deciding what goes in the cart is judgment, and stays in the conversation.
"""

from sqlmodel import desc, select

from db import session
from models import CartPush, CartPushItem


def log_push(items: list[dict], note: str | None = None) -> int:
    """Record a successful cart push. Returns the push id."""
    with session() as s:
        push = CartPush(item_count=len(items), note=note)
        s.add(push)
        s.commit()
        s.refresh(push)

        for item in items:
            s.add(
                CartPushItem(
                    push_id=push.id,
                    upc=str(item["upc"]),
                    quantity=float(item.get("quantity", 1)),
                    name=item.get("name"),
                    normalized_name=(item.get("normalized_name") or "").strip().lower() or None,
                )
            )
        s.commit()
        return push.id


def last_push(limit: int = 1) -> list[dict]:
    with session() as s:
        pushes = s.exec(select(CartPush).order_by(desc(CartPush.pushed_at)).limit(limit)).all()
        return [
            {
                "push_id": p.id,
                "pushed_at": p.pushed_at.isoformat(timespec="minutes"),
                "item_count": p.item_count,
                "note": p.note,
                "items": [
                    {"name": i.name, "normalized_name": i.normalized_name, "upc": i.upc, "quantity": i.quantity}
                    for i in s.exec(select(CartPushItem).where(CartPushItem.push_id == p.id)).all()
                ],
            }
            for p in pushes
        ]
