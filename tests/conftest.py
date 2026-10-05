import os
import sys
import tempfile
from pathlib import Path

# Point the app at a throwaway DB and fake credentials *before* db.py and kroger.py
# read them at import time. load_dotenv() won't override values already set here.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# A temp file, not sqlite:// — each in-memory connection would get its own empty DB,
# and the app opens a fresh Session per call.
_TMP_DB = Path(tempfile.mkdtemp()) / "test-shopper.db"
os.environ["SHOPPER_DB_URL"] = f"sqlite:///{_TMP_DB}"
os.environ["KROGER_CLIENT_ID"] = "test-client-id"
os.environ["KROGER_CLIENT_SECRET"] = "test-client-secret"
os.environ["SHOPPER_ZIP"] = "81224"
os.environ.pop("KROGER_USER_REFRESH_TOKEN", None)

import pytest  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402

import db  # noqa: E402
import kroger  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    """Fresh schema and cleared module-level caches for every test."""
    SQLModel.metadata.drop_all(db._engine)
    SQLModel.metadata.create_all(db._engine)
    kroger._client_token = None
    kroger._last_refresh_error = None
    kroger.SEED_REFRESH_TOKEN = ""
    yield


@pytest.fixture
def token_route(respx_mock):
    """Stub the client-credentials token endpoint, which most calls need first."""
    return respx_mock.post(kroger.TOKEN_URL).respond(
        200, json={"access_token": "client-tok", "expires_in": 1800, "token_type": "bearer"}
    )


def product_payload(upc="0000000004011", description="Bananas", price=0.69):
    return {
        "upc": upc,
        "description": description,
        "brand": "Fresh",
        "items": [{"size": "1 lb", "soldBy": "WEIGHT", "price": {"regular": price}, "inventory": {"stockLevel": "HIGH"}}],
    }


def location_payload(location_id="62000419", name="City Market - Gunnison"):
    return {"locationId": location_id, "name": name, "chain": "CITYMARKET"}
