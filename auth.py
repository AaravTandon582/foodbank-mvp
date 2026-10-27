"""Accounts, passwords and roles.

Two roles, because a food bank has two kinds of people at a keyboard:

  * **volunteer** — counts stock, records intake and distribution. The daily job.
  * **admin** — all of that, plus markets, preferences and who has an account.

Passwords are hashed with scrypt from the standard library. Not a home-made
scheme, and not a bare SHA: scrypt is deliberately slow and memory-hard, so a
stolen database cannot be run through a wordlist at speed.
"""
import functools
import hashlib
import hmac
import os
import secrets

from flask import abort, flash, g, redirect, request, session, url_for

import db
from validation import Invalid

ADMIN, VOLUNTEER = "admin", "volunteer"
ROLES = (ADMIN, VOLUNTEER)

# scrypt cost. n is the big lever: 2**15 takes ~120ms per check here, which is
# nothing for a login and a great deal of work spread over a wordlist.
# The test suite turns it down — it hashes on every fixture and is measuring
# behaviour, not cost. Never lower this in a real deployment.
_N = int(os.getenv("FOODBANK_SCRYPT_N", 2 ** 15))
_R, _P = 8, 1
_MAXMEM = 132 * 1024 * 1024

MIN_PASSWORD = 10
SESSION_KEY = "uid"


# ------------------------------------------------------------------ passwords

def hash_password(password):
    """`scrypt$n$r$p$salt$key`, all hex. Self-describing so the cost can change."""
    if len(password or "") < MIN_PASSWORD:
        raise Invalid(f"Password must be at least {MIN_PASSWORD} characters.")
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P,
                         maxmem=_MAXMEM, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${key.hex()}"


def verify_password(password, stored):
    """Constant-time check that survives a malformed or legacy hash."""
    try:
        scheme, n, r, p, salt_hex, key_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        key = hashlib.scrypt(
            (password or "").encode(), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), maxmem=_MAXMEM,
            dklen=len(key_hex) // 2,
        )
    except (ValueError, AttributeError, TypeError):
        return False
    return hmac.compare_digest(key.hex(), key_hex)


# ---------------------------------------------------------------------- users

def create_user(username, password, role=VOLUNTEER, conn=None):
    if role not in ROLES:
        raise Invalid("Unknown role.")
    digest = hash_password(password)          # validates length before any write

    def _insert(c):
        cur = c.execute(
            "INSERT OR IGNORE INTO users (username, password_hash, role) "
            "VALUES (?, ?, ?)", (username, digest, role),
        )
        if not cur.rowcount:
            raise Invalid(f"{username} already has an account.")
        return cur.lastrowid

    if conn is not None:
        return _insert(conn)
    with db.session() as c:
        return _insert(c)


def set_password(user_id, password):
    digest = hash_password(password)
    with db.session() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (digest, user_id))


def authenticate(username, password):
    """The user row, or None. Same work either way, so timing tells you nothing."""
    with db.session() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? AND active = 1", (username or "",)
        ).fetchone()

    # A missing user must cost the same as a wrong password, or the response
    # time reveals which usernames exist.
    stored = row["password_hash"] if row else hash_password(secrets.token_hex(16))
    if not verify_password(password, stored) or not row:
        return None

    with db.session() as conn:
        conn.execute(
            "UPDATE users SET last_login = datetime('now') WHERE id = ?", (row["id"],))
    return dict(row)


def any_users_exist():
    with db.session() as conn:
        return bool(conn.execute("SELECT 1 FROM users LIMIT 1").fetchone())


def bootstrap_admin():
    """Create the first admin on an empty install and return its password.

    Printed once to the console, never stored anywhere readable. Overridable
    with FOODBANK_ADMIN_PASSWORD so a deployment can set it non-interactively.
    """
    if any_users_exist():
        return None
    password = os.getenv("FOODBANK_ADMIN_PASSWORD") or secrets.token_urlsafe(16)
    create_user("admin", password, ADMIN)
    return password


# -------------------------------------------------------------------- session

def login(user):
    # A new session id on privilege change closes session-fixation.
    session.clear()
    session[SESSION_KEY] = user["id"]
    session.permanent = False


def logout():
    session.clear()


def load_current_user():
    """`g.user` for this request, or None. Wired as a before_request hook."""
    uid = session.get(SESSION_KEY)
    if not uid:
        g.user = None
        return
    with db.session() as conn:
        row = conn.execute(
            "SELECT id, username, role, active FROM users WHERE id = ? AND active = 1",
            (uid,),
        ).fetchone()
    # A deactivated account stops working immediately, not at next login.
    g.user = dict(row) if row else None
    if row is None:
        session.clear()


def current_user():
    return getattr(g, "user", None)


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            if request.method != "GET":
                abort(400, description="Your session expired. Sign in and try again.")
            flash("Please sign in.", "error")
            return redirect(url_for("login_page", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @functools.wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user()["role"] != ADMIN:
            abort(403, description="That page is for administrators.")
        return view(*args, **kwargs)
    return wrapped
