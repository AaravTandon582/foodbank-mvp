"""Lots, FEFO rotation, and the ledger that makes the numbers auditable."""
import datetime as dt

import pytest

import db
import inventory as inv
from validation import Invalid

USER = {"id": None, "username": "tester"}


_counter = iter(range(1, 10_000))


def _item(unit="case"):
    """A brand-new item with no stock, so each test controls its own lots.

    The seeded items already carry an opening-balance lot, which would make
    these assertions depend on demo data.
    """
    name = f"Test Item {next(_counter)}"
    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO items (name, category, unit, qty, par_level) "
            "VALUES (?, 'Test', ?, 0, 100)", (name, unit))
        return cur.lastrowid


def _day(offset):
    return (dt.date.today() + dt.timedelta(days=offset)).isoformat()


def _qty(item_id):
    with db.session() as conn:
        return conn.execute(
            "SELECT qty FROM items WHERE id = ?", (item_id,)).fetchone()["qty"]


# ------------------------------------------------------------------ rotation

def test_fefo_draws_the_soonest_expiring_lot_first(fresh_db):
    item = _item()
    inv.receive(item, 10, _day(60), "later", USER)
    inv.receive(item, 20, _day(5), "sooner", USER)     # arrived second, goes first
    inv.receive(item, 5, None, "non-perishable", USER)

    drawn = inv.consume(item, 25, user=USER)
    assert [d["qty"] for d in drawn] == [20, 5]
    assert drawn[0]["expires_on"] == _day(5)
    assert drawn[1]["expires_on"] == _day(60)

    remaining = {lot["expires_on"]: lot["qty"] for lot in inv.lots_for(item)}
    assert remaining == {_day(60): 5, None: 5}


def test_non_perishables_go_last(fresh_db):
    item = _item()
    inv.receive(item, 10, None, "tins", USER)
    inv.receive(item, 10, _day(30), "dated", USER)
    drawn = inv.consume(item, 10, user=USER)
    assert drawn[0]["expires_on"] == _day(30), "took the tins before the dated stock"


def test_already_expired_stock_is_drawn_first(fresh_db):
    item = _item()
    inv.receive(item, 5, _day(30), "good", USER)
    inv.receive(item, 5, _day(-3), "past date", USER)
    assert inv.consume(item, 5, user=USER)[0]["expires_on"] == _day(-3)


def test_partial_draw_leaves_the_remainder_in_place(fresh_db):
    item = _item()
    inv.receive(item, 10, _day(5), "", USER)
    inv.consume(item, 4, user=USER)
    lots = inv.lots_for(item)
    assert len(lots) == 1 and lots[0]["qty"] == 6


# -------------------------------------------------------------- bookkeeping

def test_cached_total_always_matches_the_lots(fresh_db):
    item = _item()
    inv.receive(item, 10, _day(10), "", USER)
    inv.receive(item, 7, None, "", USER)
    inv.consume(item, 3, user=USER)
    inv.adjust(item, 5, user=USER)
    inv.recount(item, 12, user=USER)
    with db.session() as conn:
        assert abs(_qty(item) - inv.on_hand(conn, item)) < 1e-9
    assert _qty(item) == 12


def test_cannot_distribute_more_than_is_on_hand(fresh_db):
    item = _item()
    inv.receive(item, 5, None, "", USER)
    with pytest.raises(Invalid):
        inv.consume(item, 6, user=USER)
    assert _qty(item) == 5, "a refused draw must change nothing"


def test_a_failed_draw_rolls_back_completely(fresh_db):
    """The refusal happens before any lot is touched."""
    item = _item()
    inv.receive(item, 3, _day(1), "", USER)
    inv.receive(item, 3, _day(2), "", USER)
    before = [(x["id"], x["qty"]) for x in inv.lots_for(item)]
    with pytest.raises(Invalid):
        inv.consume(item, 99, user=USER)
    assert [(x["id"], x["qty"]) for x in inv.lots_for(item)] == before


def test_recount_records_the_difference_not_the_total(fresh_db):
    item = _item()
    inv.receive(item, 10, None, "", USER)
    inv.recount(item, 7, user=USER)
    latest = inv.history(item)[0]
    assert latest["kind"] == inv.ADJUSTMENT
    assert latest["delta"] == -3
    assert _qty(item) == 7


def test_negative_recount_is_refused(fresh_db):
    with pytest.raises(Invalid):
        inv.recount(_item(), -1, user=USER)


def test_discarding_a_lot_empties_only_that_lot(fresh_db):
    item = _item()
    keep = inv.receive(item, 8, _day(90), "good", USER)
    toss = inv.receive(item, 4, _day(-1), "expired", USER)
    assert inv.discard_lot(toss, "past date", USER) == 4
    ids = {lot["id"]: lot["qty"] for lot in inv.lots_for(item)}
    assert ids == {keep: 8}
    assert inv.history(item)[0]["kind"] == inv.DISCARD


# ------------------------------------------------------------------- ledger

def test_the_ledger_records_who_did_it(fresh_db):
    item = _item()
    inv.receive(item, 5, None, "church drive", {"id": None, "username": "alice"})
    entry = inv.history(item)[0]
    assert entry["username"] == "alice"
    assert entry["note"] == "church drive"
    assert entry["kind"] == inv.INTAKE


def test_history_survives_the_user_being_removed(fresh_db):
    item = _item()
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM users WHERE username='vol'").fetchone()["id"]
    inv.receive(item, 5, None, "", {"id": uid, "username": "vol"})
    with db.session() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    entry = inv.history(item)[0]
    assert entry["username"] == "vol", "the audit trail lost who did it"


def test_every_movement_is_logged(fresh_db):
    item = _item()
    before = len(inv.history(item, limit=500))
    inv.receive(item, 10, _day(9), "", USER)
    inv.consume(item, 2, user=USER)
    inv.adjust(item, 3, user=USER)
    after = inv.history(item, limit=500)
    assert len(after) == before + 3
    assert sum(1 for r in after if r["kind"] == inv.INTAKE) >= 1


# ------------------------------------------------------------------- expiry

def test_expiring_lists_soonest_first_and_flags_the_past(fresh_db):
    item = _item()
    inv.receive(item, 1, _day(20), "", USER)
    inv.receive(item, 1, _day(-2), "", USER)
    inv.receive(item, 1, _day(5), "", USER)
    inv.receive(item, 1, None, "never", USER)

    rows = inv.expiring(within_days=30)
    assert [r["days_left"] for r in rows] == sorted(r["days_left"] for r in rows)
    assert rows[0]["expired"] is True
    assert all(r["expires_on"] for r in rows), "an undated lot leaked into the list"


def test_expiring_respects_the_horizon(fresh_db):
    item = _item()
    inv.receive(item, 1, _day(400), "", USER)
    assert inv.expiring(within_days=30) == []
    assert len(inv.expiring(within_days=500)) == 1


def test_emptied_lots_drop_off_the_expiry_list(fresh_db):
    item = _item()
    inv.receive(item, 3, _day(2), "", USER)
    inv.consume(item, 3, user=USER)
    assert inv.expiring(within_days=30) == []


def test_opening_balances_are_idempotent(fresh_db):
    """Safe to run on every start — it must not double-count."""
    before = _qty(_item())
    assert inv.opening_balances() == 0
    assert _qty(_item()) == before


# ------------------------------------------------------------------- routes

def test_intake_and_distribute_through_the_web(client):
    item = _item()
    tok = client.get("/").get_data(as_text=True).split(
        'name="_csrf" value="')[1].split('"')[0]
    client.post(f"/items/{item}/intake",
                data={"qty": "24", "expires_on": _day(14), "note": "drive", "_csrf": tok})
    assert any(lot["expires_on"] == _day(14) for lot in inv.lots_for(item))

    client.post(f"/items/{item}/distribute", data={"qty": "4", "_csrf": tok})
    assert inv.history(item)[0]["kind"] == inv.DISTRIBUTION

    body = client.get(f"/items/{item}").get_data(as_text=True)
    assert "next out" in body


def test_bad_expiry_date_is_a_400(client):
    tok = client.get("/").get_data(as_text=True).split(
        'name="_csrf" value="')[1].split('"')[0]
    r = client.post(f"/items/{_item()}/intake",
                    data={"qty": "1", "expires_on": "not-a-date", "_csrf": tok})
    assert r.status_code == 400
