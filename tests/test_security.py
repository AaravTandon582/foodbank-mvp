"""The things that would let someone else drive this app."""
import security


def test_csrf_blocks_forged_post(client):
    """A POST with no token is refused — this is the cross-site request case."""
    before = client.get("/").get_data(as_text=True)
    assert client.post("/markets/1/delete").status_code == 400
    assert client.post("/items", data={"name": "Injected"}).status_code == 400
    assert "Injected" not in before


def test_csrf_allows_real_form(client, token):
    r = client.post("/items", data={"name": "Lentils", "_csrf": token})
    assert r.status_code == 302
    assert "Lentils" in client.get("/").get_data(as_text=True)


def test_csrf_rejects_a_wrong_token(client, token):
    r = client.post("/items", data={"name": "Nope", "_csrf": token[:-1] + "x"})
    assert r.status_code == 400


def test_referrer_cannot_redirect_off_site(client, token):
    """`redirect(request.referrer)` was an open redirect."""
    r = client.post("/items/1/qty", data={"delta": "1", "_csrf": token},
                    headers={"Referer": "https://evil.example.com/phish"})
    assert r.status_code == 302
    assert "evil.example.com" not in r.headers["Location"]


def test_safe_redirect_predicate():
    with __import__("app").app.test_request_context("/", base_url="http://localhost"):
        assert security.is_safe_redirect("/shopping")
        assert security.is_safe_redirect("http://localhost/markets")
        assert not security.is_safe_redirect("https://evil.example.com/x")
        assert not security.is_safe_redirect("//evil.example.com")
        assert not security.is_safe_redirect("javascript:alert(1)")
        assert not security.is_safe_redirect(None)


def test_security_headers_present(client):
    r = client.get("/")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]


def test_no_inline_script_would_violate_the_csp(client):
    """The CSP forbids inline script, so no template may carry one."""
    for path in ("/", "/shopping", "/markets"):
        body = client.get(path).get_data(as_text=True)
        assert "onclick=" not in body
        assert "oninput=" not in body


def test_item_name_is_escaped(client, token):
    client.post("/items", data={"name": "<script>alert(1)</script>", "_csrf": token})
    body = client.get("/").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_session_cookie_is_hardened():
    from app import app
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
