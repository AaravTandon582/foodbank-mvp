"""Test fixtures. Every test runs against a throwaway database."""
import os
import sys
import tempfile
from pathlib import Path

# Point the app at a temp database BEFORE importing it — db.DB_PATH is read
# at import time, and no test should ever touch the real foodbank.db.
_TMP = Path(tempfile.mkdtemp(prefix="pantryos-test-"))
os.environ["FOODBANK_DB"] = str(_TMP / "test.db")
os.environ["FOODBANK_SECRET_KEY"] = "test-key-not-a-real-secret"
# Password hashing is deliberately slow; tests hash constantly and are
# checking behaviour, not cost.
os.environ["FOODBANK_SCRYPT_N"] = "1024"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import auth  # noqa: E402
import db  # noqa: E402
import inventory  # noqa: E402
import seed  # noqa: E402
from app import app as flask_app  # noqa: E402

ADMIN_PW = "test-admin-password"
VOLUNTEER_PW = "test-volunteer-pw"


@pytest.fixture()
def fresh_db():
    if db.DB_PATH.exists():
        db.DB_PATH.unlink()
    db.init()
    seed.seed_if_empty()
    inventory.opening_balances()
    auth.create_user("admin", ADMIN_PW, auth.ADMIN)
    auth.create_user("vol", VOLUNTEER_PW, auth.VOLUNTEER)
    return db


@pytest.fixture()
def anon(fresh_db):
    """A client that has not signed in."""
    flask_app.config.update(TESTING=False)
    with flask_app.test_client() as c:
        yield c


def _sign_in(c, username, password):
    page = c.get("/login").get_data(as_text=True)
    tok = page.split('name="_csrf" value="')[1].split('"')[0]
    r = c.post("/login", data={"username": username, "password": password, "_csrf": tok})
    assert r.status_code == 302, "sign-in failed"
    return c


@pytest.fixture()
def client(fresh_db):
    """Signed in as an administrator — the default for most tests."""
    flask_app.config.update(TESTING=False)
    with flask_app.test_client() as c:
        yield _sign_in(c, "admin", ADMIN_PW)


@pytest.fixture()
def volunteer(fresh_db):
    flask_app.config.update(TESTING=False)
    with flask_app.test_client() as c:
        yield _sign_in(c, "vol", VOLUNTEER_PW)


@pytest.fixture()
def token(client):
    """A valid CSRF token for the client's session."""
    client.get("/")
    with client.session_transaction() as sess:
        import security
        return sess.get(security.TOKEN_FIELD)
