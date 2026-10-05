from datetime import date, datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class Profile(SQLModel, table=True):
    """Single-row household shopping context."""

    id: Optional[int] = Field(default=1, primary_key=True)
    zip_code: Optional[str] = None
    store_name: Optional[str] = None
    location_id: Optional[str] = None
    health_profile: Optional[str] = None
    shopping_notes: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.now)


class KrogerToken(SQLModel, table=True):
    """Single-row user OAuth token for cart writes."""

    id: Optional[int] = Field(default=1, primary_key=True)
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    expires_at: Optional[datetime] = None


class Receipt(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    store: str
    purchased_on: date
    total: Optional[float] = None
    created_at: datetime = Field(default_factory=datetime.now)


class ReceiptItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    receipt_id: int = Field(foreign_key="receipt.id", index=True)
    name: str
    normalized_name: str = Field(index=True)
    quantity: float = 1.0
    unit_price: Optional[float] = None
    upc: Optional[str] = None
    category: Optional[str] = None


class CartPush(SQLModel, table=True):
    """A batch of items sent to the Kroger cart.

    The Kroger API can't read the cart back, so this log is the only record of what
    was ordered — it also stops a weekly fill from being pushed twice.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    pushed_at: datetime = Field(default_factory=datetime.now)
    item_count: int = 0
    note: Optional[str] = None


class CartPushItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    push_id: int = Field(foreign_key="cartpush.id", index=True)
    normalized_name: Optional[str] = None
    name: Optional[str] = None
    upc: str
    quantity: float = 1.0


class KnownItem(SQLModel, table=True):
    """UPC cache — lets repeat staples skip product search."""

    normalized_name: str = Field(primary_key=True)
    upc: str
    description: Optional[str] = None
    brand: Optional[str] = None
    size: Optional[str] = None
    last_seen: date = Field(default_factory=date.today)
