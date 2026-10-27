"""Request-level protection: CSRF tokens and response headers.

Written against the standard library on purpose — the app's selling point is
that it runs with nothing installed, and adding Flask-WTF for one token would
cost that.

The CSRF scheme is the signed double-submit pattern: a random secret lives in
the session cookie, every form carries a token derived from it, and a POST
without a matching token is refused. That is what stops a page on another site
from quietly POSTing to `/items/3/delete` while a volunteer is logged in.
"""
import hmac
import os
import secrets
import stat
from pathlib import Path

from flask import abort, request, session

TOKEN_FIELD = "_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

# No inline script or style is used anywhere, with one exception: the range
# slider's `oninput`. That is why 'unsafe-inline' appears for style-src only
# and the slider was moved to an external file.
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "object-src 'none'"
)

HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


def load_secret_key(path):
    """A stable signing key: from the environment, or a 0600 file beside the db.

    A key regenerated on every boot would log everyone out on restart and make
    CSRF failures look random, so it is persisted — but never committed.
    """
    from_env = os.getenv("FOODBANK_SECRET_KEY")
    if from_env:
        return from_env.encode()

    path = Path(path)
    if path.exists():
        data = path.read_bytes().strip()
        if data:
            return data

    key = secrets.token_bytes(32)
    path.write_bytes(key)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600: owner only
    return key


def csrf_token():
    """The token for this session, minted on first use."""
    token = session.get(TOKEN_FIELD)
    if not token:
        token = secrets.token_urlsafe(32)
        session[TOKEN_FIELD] = token
    return token


def check_csrf():
    """Refuse an unsafe request whose token is missing or wrong."""
    if request.method in SAFE_METHODS:
        return
    expected = session.get(TOKEN_FIELD)
    supplied = request.form.get(TOKEN_FIELD) or request.headers.get("X-CSRF-Token", "")
    if not expected or not hmac.compare_digest(str(expected), str(supplied)):
        abort(400, description="Your form expired or came from another site. Reload and try again.")


def is_safe_redirect(target):
    """True only for a same-origin relative path.

    `redirect(request.referrer)` is an open redirect: the Referer header is set
    by whoever sent the request, so it can point anywhere.
    """
    if not target:
        return False
    from urllib.parse import urlparse
    parts = urlparse(target)
    if parts.scheme and parts.scheme not in ("http", "https"):
        return False
    if parts.netloc and parts.netloc != request.host:
        return False
    return not target.startswith("//")


def install(app, secret_path):
    """Wire CSRF, headers and a hardened session cookie onto an app."""
    app.secret_key = load_secret_key(secret_path)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("FOODBANK_HTTPS") == "1",
        # Forms are small; anything larger is not a food bank inventory update.
        MAX_CONTENT_LENGTH=256 * 1024,
    )

    app.before_request(check_csrf)
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.after_request
    def _headers(response):
        for name, value in HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    return app
