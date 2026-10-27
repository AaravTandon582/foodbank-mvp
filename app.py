"""Food bank inventory + market price finder (MVP).

Run:  python3 app.py    then open http://127.0.0.1:5000

Security posture, in short: every form is CSRF-protected, every number is
validated before it reaches the database, redirects are same-origin only, and
the debugger is off unless you explicitly ask for it.
"""
import os
import time
from pathlib import Path

from flask import (Flask, abort, flash, redirect, render_template, request,
                   url_for)

import auth
import db
import inventory as stock
import refresh as refresh_mod
import security
import seed
import shopping
from validation import Invalid, date as _date, number, text

app = Flask(__name__)
security.install(app, Path(__file__).parent / ".secret_key")

# Every request knows who is making it before any view runs.
app.before_request(auth.load_current_user)
app.jinja_env.globals["current_user"] = auth.current_user
app.jinja_env.globals["ADMIN"] = auth.ADMIN

# Bounds the browser is allowed to set. A par level of 1e308 is not a typo the
# app should store; it is a number that breaks every total downstream.
MAX_QTY = 1_000_000.0

# How far ahead the expiry dashboard looks by default.
EXPIRY_HORIZON = 30

# Rescanning hits live third-party APIs. A held-down button should not turn a
# food bank into a source of unwanted traffic.
SCAN_COOLDOWN = 30.0
_last_scan = 0.0


@app.template_filter("money")
def money(value):
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _back(endpoint="inventory"):
    """Return to the referring page, but only if it is ours."""
    target = request.referrer
    return redirect(target if security.is_safe_redirect(target) else url_for(endpoint))


# ------------------------------------------------------------- error handling

@app.errorhandler(Invalid)
def _bad_input(exc):
    flash(str(exc), "error")
    return _back(), 400


@app.errorhandler(400)
def _bad_request(exc):
    return render_template(
        "error.html", code=400,
        message=getattr(exc, "description", "That request could not be understood."),
    ), 400


@app.errorhandler(403)
def _forbidden(exc):
    return render_template(
        "error.html", code=403,
        message=getattr(exc, "description", "You do not have access to that."),
    ), 403


@app.errorhandler(404)
def _not_found(exc):
    return render_template("error.html", code=404,
                           message="No such page."), 404


@app.errorhandler(500)
def _server_error(exc):
    # Never render the exception: it is the one thing an attacker wants to see.
    app.logger.exception("unhandled error")
    return render_template(
        "error.html", code=500,
        message="Something went wrong on our side. The error has been logged.",
    ), 500


# --------------------------------------------------------------------- auth

@app.get("/login")
def login_page():
    if auth.current_user():
        return redirect(url_for("inventory"))
    return render_template("login.html")


@app.post("/login")
def do_login():
    user = auth.authenticate(
        text(request.form, "username", limit=40),
        request.form.get("password", ""),
    )
    if not user:
        # Never say which half was wrong.
        flash("Wrong username or password.", "error")
        return redirect(url_for("login_page"))
    auth.login(user)
    target = request.form.get("next") or url_for("inventory")
    return redirect(target if security.is_safe_redirect(target) else url_for("inventory"))


@app.post("/logout")
def do_logout():
    auth.logout()
    flash("Signed out.", "ok")
    return redirect(url_for("login_page"))


@app.get("/users")
@auth.admin_required
def users_page():
    with db.session() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, username, role, active, created_at, last_login "
            "FROM users ORDER BY active DESC, username")]
    return render_template("users.html", users=rows)


@app.post("/users")
@auth.admin_required
def add_user():
    name = text(request.form, "username", limit=40, required=True)
    role = request.form.get("role", auth.VOLUNTEER)
    auth.create_user(name, request.form.get("password", ""), role)
    flash(f"Account created for {name}.", "ok")
    return redirect(url_for("users_page"))


@app.post("/users/<int:user_id>/toggle")
@auth.admin_required
def toggle_user(user_id):
    me = auth.current_user()
    if user_id == me["id"]:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("users_page"))
    with db.session() as conn:
        # Never leave the install with no way back in.
        admins = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE role = ? AND active = 1",
            (auth.ADMIN,)).fetchone()["c"]
        row = conn.execute(
            "SELECT role, active FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            flash("No such account.", "error")
        elif row["active"] and row["role"] == auth.ADMIN and admins <= 1:
            flash("That is the last administrator.", "error")
        else:
            conn.execute(
                "UPDATE users SET active = 1 - active WHERE id = ?", (user_id,))
    return redirect(url_for("users_page"))


@app.post("/users/<int:user_id>/password")
@auth.admin_required
def reset_password(user_id):
    auth.set_password(user_id, request.form.get("password", ""))
    flash("Password updated.", "ok")
    return redirect(url_for("users_page"))


# ---------------------------------------------------------------- inventory

@app.get("/")
@auth.login_required
def inventory():
    with db.session() as conn:
        items = [dict(r) for r in conn.execute(
            "SELECT * FROM items ORDER BY category, name"
        )]
    for item in items:
        item["short"] = item["qty"] < item["par_level"]
        item["pct"] = min(100, item["qty"] / item["par_level"] * 100) if item["par_level"] else 100
    by_category = {}
    for item in items:
        by_category.setdefault(item["category"], []).append(item)
    summary = stock.expiry_summary(EXPIRY_HORIZON)
    return render_template(
        "inventory.html",
        by_category=by_category,
        low_count=sum(1 for i in items if i["short"]),
        total=len(items),
        expired_count=len(summary["expired"]),
        soon_count=len(summary["soon"]),
    )


@app.post("/items")
@auth.login_required
def add_item():
    form = request.form
    name = text(form, "name", limit=80, required=True)
    with db.session() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO items (name, category, unit, qty, par_level) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                name,
                text(form, "category", "Other"),
                text(form, "unit", "each"),
                number(form, "qty", 0.0, low=0.0, high=MAX_QTY),
                number(form, "par_level", 0.0, low=0.0, high=MAX_QTY),
            ),
        )
        if not cur.rowcount:
            flash(f"{name} is already on the list.", "error")
    return redirect(url_for("inventory"))


@app.post("/items/<int:item_id>/qty")
@auth.login_required
def update_qty(item_id):
    """The +/- buttons and the par-level edit. Quantity moves via the ledger."""
    form = request.form
    user = auth.current_user()
    if "delta" in form:
        delta = number(form, "delta", low=-MAX_QTY, high=MAX_QTY, required=True)
        try:
            stock.adjust(item_id, delta, user=user)
        except Invalid as exc:
            # Counting down past zero is a slip, not worth a red error page.
            flash(str(exc), "error")
    else:
        stock.recount(
            item_id, number(form, "qty", low=0.0, high=MAX_QTY, required=True), user=user)
        with db.session() as conn:
            conn.execute(
                "UPDATE items SET par_level = ? WHERE id = ?",
                (number(form, "par_level", low=0.0, high=MAX_QTY, required=True), item_id),
            )
    return _back()


@app.post("/items/<int:item_id>/intake")
@auth.login_required
def item_intake(item_id):
    """Stock arriving: a donation or a purchase, with a date it goes off."""
    form = request.form
    lot_id = stock.receive(
        item_id,
        number(form, "qty", low=0.01, high=MAX_QTY, required=True),
        expires_on=_date(form, "expires_on"),
        note=text(form, "note", limit=120),
        user=auth.current_user(),
    )
    flash("Intake recorded." if lot_id else "Nothing recorded.", "ok")
    return _back()


@app.post("/items/<int:item_id>/distribute")
@auth.login_required
def item_distribute(item_id):
    """Stock going out, drawn earliest-expiring first."""
    form = request.form
    drawn = stock.consume(
        item_id,
        number(form, "qty", low=0.01, high=MAX_QTY, required=True),
        kind=stock.DISTRIBUTION,
        note=text(form, "note", limit=120),
        user=auth.current_user(),
    )
    dated = [d for d in drawn if d["expires_on"]]
    flash(
        f"Distributed from {len(drawn)} lot(s)"
        + (f", earliest expiring {dated[0]['expires_on']}." if dated else "."),
        "ok",
    )
    return _back()


@app.post("/lots/<int:lot_id>/discard")
@auth.login_required
def discard_lot(lot_id):
    qty = stock.discard_lot(
        lot_id, note=text(request.form, "note", limit=120), user=auth.current_user())
    flash(f"Wrote off {qty:g} units.", "ok")
    return _back("expiring_page")


@app.get("/items/<int:item_id>")
@auth.login_required
def item_detail(item_id):
    with db.session() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        abort(404)
    return render_template(
        "item.html", item=dict(row),
        lots=stock.lots_for(item_id), history=stock.history(item_id, limit=50),
    )


@app.get("/expiring")
@auth.login_required
def expiring_page():
    days = int(number(request.args, "days", EXPIRY_HORIZON, low=1, high=3650))
    return render_template("expiring.html", summary=stock.expiry_summary(days))


@app.get("/history")
@auth.login_required
def history_page():
    return render_template("history.html", rows=stock.history(limit=200))


@app.post("/items/<int:item_id>/delete")
@auth.admin_required
def delete_item(item_id):
    with db.session() as conn:
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    return redirect(url_for("inventory"))


# ----------------------------------------------------------------- shopping

@app.get("/shopping")
@auth.login_required
def shopping_list():
    return render_template("shopping.html", plan=shopping.build_plan())


@app.post("/scan")
@auth.login_required
def scan():
    """Re-query real price sources for the items running low."""
    global _last_scan
    waited = time.monotonic() - _last_scan
    if waited < SCAN_COOLDOWN:
        flash(f"Just scanned. Try again in {SCAN_COOLDOWN - waited:.0f}s.", "error")
        return redirect(url_for("shopping_list"))

    names = [item["name"] for item in shopping.low_stock()]
    if not names:
        flash("Everything is at par — nothing to price.", "ok")
        return redirect(url_for("shopping_list"))

    _last_scan = time.monotonic()
    try:
        report = refresh_mod.refresh(names)
    except Exception:
        # A flaky store API must not take the page down with it.
        app.logger.exception("price refresh failed")
        flash("Could not reach the price sources. Cached prices are unchanged.", "error")
        return redirect(url_for("shopping_list"))

    failed = [n for n, r in report["sources"].items() if isinstance(r, str)]
    parts = [f"Updated {report['written']} real prices"]
    if report["demo_filled"]:
        parts.append(f"filled {report['demo_filled']} gaps with demo prices")
    if report["rejected"]:
        parts.append(f"discarded {report['rejected']} implausible ones")
    note = ", ".join(parts)
    if failed:
        note += f". {len(failed)} source(s) unavailable: {', '.join(failed)}"
    flash(note + ".", "error" if failed and not report["written"] else "ok")
    if report["new_stores"]:
        flash(
            "New stores found, switched off until you set a distance: "
            + ", ".join(report["new_stores"]), "ok",
        )
    return redirect(url_for("shopping_list"))


# ------------------------------------------------------ markets & settings

@app.get("/markets")
@auth.login_required
def market_settings():
    with db.session() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT m.*, COUNT(p.id) AS priced, MAX(p.checked_at) AS last_check "
            "FROM markets m LEFT JOIN prices p ON p.market_id = m.id "
            "GROUP BY m.id ORDER BY m.distance"
        )]
    return render_template("markets.html", markets=rows, settings=db.get_settings())


@app.post("/markets")
@auth.admin_required
def add_market():
    name = text(request.form, "name", limit=80, required=True)
    with db.session() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO markets (name, distance) VALUES (?, ?)",
            (name, number(request.form, "distance", 0.0, low=0.0, high=500.0)),
        )
        if not cur.rowcount:
            flash(f"{name} is already a market.", "error")
    return redirect(url_for("market_settings"))


@app.post("/markets/<int:market_id>/toggle")
@auth.admin_required
def toggle_market(market_id):
    with db.session() as conn:
        conn.execute("UPDATE markets SET enabled = 1 - enabled WHERE id = ?", (market_id,))
    return redirect(url_for("market_settings"))


@app.post("/markets/<int:market_id>/delete")
@auth.admin_required
def delete_market(market_id):
    with db.session() as conn:
        conn.execute("DELETE FROM markets WHERE id = ?", (market_id,))
    return redirect(url_for("market_settings"))


@app.post("/settings")
@auth.admin_required
def update_settings():
    form = request.form
    db.save_settings({
        "price_weight": number(form, "price_weight", low=0, high=100, required=True) / 100,
        "max_distance": number(form, "max_distance", low=0, high=500, required=True),
        "cost_per_mile": number(form, "cost_per_mile", low=0, high=20, required=True),
        "price_max_age_days": number(form, "price_max_age_days", 14, low=1, high=3650),
    })
    flash("Preferences saved.", "ok")
    return redirect(url_for("market_settings"))


def create_app():
    """Entry point for a real WSGI server, which never runs __main__."""
    db.init()
    seed.seed_if_empty()
    # Pre-ledger stock gets an opening lot so the totals and the ledger agree.
    stock.opening_balances()
    password = auth.bootstrap_admin()
    if password:
        print("\n" + "=" * 62)
        print("  First run: an administrator account has been created.")
        print("     username: admin")
        print(f"     password: {password}")
        print("  This is shown once. Sign in and change it.")
        print("=" * 62 + "\n")
    return app


if __name__ == "__main__":
    create_app()
    # The Werkzeug debugger is a remote shell for anyone who can reach it.
    # Off unless deliberately switched on, and bound to localhost either way.
    app.run(
        debug=os.getenv("FOODBANK_DEBUG") == "1",
        host=os.getenv("FOODBANK_HOST", "127.0.0.1"),
        port=int(os.getenv("FOODBANK_PORT", "5000")),
    )
