"""Accounts, passwords, roles, and the routes they guard."""
import auth
import db
from validation import Invalid

import pytest


def test_password_round_trip():
    h = auth.hash_password("correct-horse-battery")
    assert auth.verify_password("correct-horse-battery", h)
    assert not auth.verify_password("Correct-horse-battery", h)
    assert not auth.verify_password("", h)


def test_password_is_not_stored_in_the_clear(fresh_db):
    with db.session() as conn:
        stored = conn.execute(
            "SELECT password_hash FROM users WHERE username = 'admin'").fetchone()[0]
    from tests.conftest import ADMIN_PW
    assert ADMIN_PW not in stored
    assert stored.startswith("scrypt$")


def test_each_hash_is_salted(fresh_db):
    a, b = auth.hash_password("same-password-x"), auth.hash_password("same-password-x")
    assert a != b, "identical passwords produced identical hashes: no salt"


def test_short_passwords_are_refused():
    with pytest.raises(Invalid):
        auth.hash_password("short")


def test_malformed_stored_hash_does_not_crash():
    for junk in ("", "nonsense", "scrypt$bad", None, "scrypt$x$y$z$q$r"):
        assert auth.verify_password("anything", junk) is False


def test_authenticate(fresh_db):
    from tests.conftest import ADMIN_PW
    assert auth.authenticate("admin", ADMIN_PW)["role"] == auth.ADMIN
    assert auth.authenticate("admin", "wrong") is None
    assert auth.authenticate("nobody", "whatever") is None


def test_deactivated_account_cannot_sign_in(fresh_db):
    from tests.conftest import VOLUNTEER_PW
    with db.session() as conn:
        conn.execute("UPDATE users SET active = 0 WHERE username = 'vol'")
    assert auth.authenticate("vol", VOLUNTEER_PW) is None


def test_every_page_requires_a_login(anon):
    for path in ("/", "/shopping", "/markets", "/expiring", "/history", "/users"):
        r = anon.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"], path


def test_unauthenticated_writes_are_refused(anon):
    """No token and no session: the write must not happen."""
    assert anon.post("/items", data={"name": "Ghost"}).status_code == 400
    with db.session() as conn:
        assert conn.execute(
            "SELECT 1 FROM items WHERE name = 'Ghost'").fetchone() is None


def test_volunteer_cannot_reach_admin_pages(volunteer):
    assert volunteer.get("/users").status_code == 403
    assert volunteer.post("/settings", data={
        "price_weight": "50", "max_distance": "10", "cost_per_mile": "1",
        "_csrf": _token(volunteer)}).status_code == 403


def test_volunteer_can_do_the_daily_job(volunteer):
    r = volunteer.post("/items/1/qty", data={"delta": "5", "_csrf": _token(volunteer)})
    assert r.status_code == 302
    assert volunteer.get("/").status_code == 200
    assert volunteer.get("/expiring").status_code == 200


def test_admin_can_reach_admin_pages(client):
    assert client.get("/users").status_code == 200


def test_deactivating_the_last_admin_is_refused(client):
    with db.session() as conn:
        uid = conn.execute(
            "SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
        conn.execute("UPDATE users SET role = 'volunteer' WHERE username = 'vol'")
    # Cannot deactivate yourself...
    client.post(f"/users/{uid}/toggle", data={"_csrf": _token(client)})
    with db.session() as conn:
        assert conn.execute(
            "SELECT active FROM users WHERE id = ?", (uid,)).fetchone()["active"] == 1


def test_login_does_not_reveal_which_half_was_wrong(anon):
    tok = _token(anon, "/login")
    a = anon.post("/login", data={"username": "admin", "password": "x", "_csrf": tok},
                  follow_redirects=True).get_data(as_text=True)
    tok = _token(anon, "/login")
    b = anon.post("/login", data={"username": "ghost", "password": "x", "_csrf": tok},
                  follow_redirects=True).get_data(as_text=True)
    assert "Wrong username or password." in a
    assert a == b, "the two failures are distinguishable"


def test_session_is_replaced_on_login(anon):
    """Session fixation: the pre-login session must not survive."""
    anon.get("/login")
    with anon.session_transaction() as sess:
        sess["planted"] = "attacker-value"
    from tests.conftest import ADMIN_PW, _sign_in
    _sign_in(anon, "admin", ADMIN_PW)
    with anon.session_transaction() as sess:
        assert "planted" not in sess


def test_signing_out_clears_the_session(client):
    client.post("/logout", data={"_csrf": _token(client)})
    assert client.get("/").status_code == 302


def _token(c, path="/"):
    page = c.get(path).get_data(as_text=True)
    return page.split('name="_csrf" value="')[1].split('"')[0]
