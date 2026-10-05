import os
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
from sqlmodel import Session, SQLModel, create_engine

import models  # noqa: F401  — registers tables on SQLModel.metadata

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

DB_URL = os.getenv("SHOPPER_DB_URL") or f"sqlite:///{ROOT / 'shopper.db'}"

_engine = create_engine(DB_URL)


def init_db() -> None:
    SQLModel.metadata.create_all(_engine)


@contextmanager
def session():
    with Session(_engine) as s:
        yield s
