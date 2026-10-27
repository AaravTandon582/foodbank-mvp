"""Stock movement: lots, expiry rotation, and the ledger behind both.

The rule this file exists to enforce: **nothing writes `items.qty` directly.**
Every change goes through this module, which moves stock in or out of dated
lots, writes a ledger row saying who did it and why, and recomputes the item
total from the lots. That is what makes the number on the inventory page
something you can audit rather than something someone typed.

Rotation is FEFO — first expired, first out. Not FIFO: the tin that arrived
first is not necessarily the tin that goes off first, and it is the expiry
date that decides whether food can be handed to someone.
"""
import db
from validation import Invalid

INTAKE = "intake"              # donation or purchase arriving
DISTRIBUTION = "distribution"  # going out to a household
ADJUSTMENT = "adjustment"      # a recount correcting the books
DISCARD = "discard"            # expired or spoiled, written off
KINDS = (INTAKE, DISTRIBUTION, ADJUSTMENT, DISCARD)

KIND_LABEL = {
    INTAKE: "Intake", DISTRIBUTION: "Distributed",
    ADJUSTMENT: "Recount", DISCARD: "Discarded",
}

# FEFO order: soonest expiry first, non-perishables last, oldest stock first
# as the tie-break.
FEFO = "ORDER BY (expires_on IS NULL), expires_on ASC, received_on ASC, id ASC"


def _resync(conn, item_id):
    """Recompute the cached item total from its lots. The only writer of qty."""
    conn.execute(
        "UPDATE items SET qty = COALESCE("
        "  (SELECT SUM(qty) FROM lots WHERE item_id = ?), 0"
        "), updated_at = datetime('now') WHERE id = ?",
        (item_id, item_id),
    )


def _log(conn, item_id, lot_id, delta, kind, user, note):
    conn.execute(
        "INSERT INTO transactions (item_id, lot_id, delta, kind, user_id, username, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (item_id, lot_id, delta, kind,
         (user or {}).get("id"), (user or {}).get("username", "system"), note),
    )


def on_hand(conn, item_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(qty), 0) AS q FROM lots WHERE item_id = ?", (item_id,)
    ).fetchone()
    return row["q"]


# ------------------------------------------------------------------- movement

def receive(item_id, qty, expires_on=None, note="", user=None, conn=None):
    """Stock arriving: a new dated lot plus an intake row."""
    if qty <= 0:
        raise Invalid("Intake quantity must be more than zero.")

    def _run(c):
        if not c.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
            raise Invalid("No such item.")
        cur = c.execute(
            "INSERT INTO lots (item_id, qty, expires_on, note) VALUES (?, ?, ?, ?)",
            (item_id, qty, expires_on or None, note),
        )
        _log(c, item_id, cur.lastrowid, qty, INTAKE, user, note)
        _resync(c, item_id)
        return cur.lastrowid

    if conn is not None:
        return _run(conn)
    with db.session() as c:
        return _run(c)


def consume(item_id, qty, kind=DISTRIBUTION, note="", user=None, conn=None):
    """Draw `qty` out of stock, earliest-expiring lot first.

    Returns the lots it drew from, so the caller can show what actually left
    the shelf. Refuses to go negative: you cannot hand out food you do not
    have, and silently allowing it would make the ledger fiction.
    """
    if qty <= 0:
        raise Invalid("Quantity must be more than zero.")

    def _run(c):
        available = on_hand(c, item_id)
        if qty > available + 1e-9:
            raise Invalid(f"Only {available:g} on hand — cannot take {qty:g} out.")
        remaining, drawn = qty, []
        rows = c.execute(
            f"SELECT id, qty, expires_on FROM lots WHERE item_id = ? AND qty > 0 {FEFO}",
            (item_id,),
        ).fetchall()
        for lot in rows:
            if remaining <= 1e-9:
                break
            take = min(lot["qty"], remaining)
            c.execute("UPDATE lots SET qty = qty - ? WHERE id = ?", (take, lot["id"]))
            _log(c, item_id, lot["id"], -take, kind, user, note)
            drawn.append(
                {"lot_id": lot["id"], "qty": take, "expires_on": lot["expires_on"]})
            remaining -= take
        _resync(c, item_id)
        return drawn

    if conn is not None:
        return _run(conn)
    with db.session() as c:
        return _run(c)


def adjust(item_id, delta, kind=ADJUSTMENT, note="", user=None):
    """Nudge stock up or down by `delta`, the +/- buttons' path.

    Up goes into an undated 'counted in' lot, because a recount has no
    delivery date to attach it to. Down draws FEFO like anything else.
    """
    if delta == 0:
        return []
    with db.session() as conn:
        if delta > 0:
            row = conn.execute(
                "SELECT id FROM lots WHERE item_id = ? AND expires_on IS NULL "
                "ORDER BY id DESC LIMIT 1", (item_id,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE lots SET qty = qty + ? WHERE id = ?", (delta, row["id"]))
                lot_id = row["id"]
            else:
                lot_id = conn.execute(
                    "INSERT INTO lots (item_id, qty, note) VALUES (?, ?, 'counted in')",
                    (item_id, delta),
                ).lastrowid
            _log(conn, item_id, lot_id, delta, kind, user, note)
            _resync(conn, item_id)
            return [{"lot_id": lot_id, "qty": delta, "expires_on": None}]
        return consume(item_id, -delta, kind, note, user, conn=conn)


def recount(item_id, new_qty, note="", user=None):
    """Set the absolute on-hand figure, logging the difference."""
    if new_qty < 0:
        raise Invalid("Count cannot be negative.")
    with db.session() as conn:
        delta = new_qty - on_hand(conn, item_id)
    return adjust(item_id, delta, ADJUSTMENT, note or "recount", user)


def discard_lot(lot_id, note="", user=None):
    """Write off one whole lot — the expired-stock path."""
    with db.session() as conn:
        lot = conn.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)).fetchone()
        if not lot:
            raise Invalid("No such lot.")
        if lot["qty"] <= 0:
            raise Invalid("That lot is already empty.")
        conn.execute("UPDATE lots SET qty = 0 WHERE id = ?", (lot_id,))
        _log(conn, lot["item_id"], lot_id, -lot["qty"], DISCARD, user, note or "expired")
        _resync(conn, lot["item_id"])
        return lot["qty"]


# -------------------------------------------------------------------- reading

def lots_for(item_id):
    with db.session() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT *, CAST(julianday(expires_on) - julianday(date('now')) AS INTEGER) "
            "AS days_left "
            f"FROM lots WHERE item_id = ? AND qty > 0 {FEFO}", (item_id,))]


def expiring(within_days=30, limit=200):
    """Open lots with a date, soonest first. Negative days_left is expired."""
    with db.session() as conn:
        rows = conn.execute(
            "SELECT l.*, i.name AS item, i.unit, i.category, "
            "       CAST(julianday(l.expires_on) - julianday(date('now')) "
            "            AS INTEGER) AS days_left "
            "FROM lots l JOIN items i ON i.id = l.item_id "
            "WHERE l.qty > 0 AND l.expires_on IS NOT NULL "
            "  AND julianday(l.expires_on) - julianday(date('now')) <= ? "
            "ORDER BY l.expires_on ASC, i.name LIMIT ?",
            (within_days, limit),
        ).fetchall()
    out = [dict(r) for r in rows]
    for lot in out:
        lot["expired"] = lot["days_left"] < 0
    return out


def expiry_summary(within_days=30):
    lots = expiring(within_days)
    expired = [x for x in lots if x["expired"]]
    return {
        "lots": lots,
        "expired": expired,
        "soon": [x for x in lots if not x["expired"]],
        "expired_units": sum(x["qty"] for x in expired),
        "within_days": within_days,
    }


def history(item_id=None, limit=100):
    sql = ("SELECT t.*, i.name AS item, i.unit FROM transactions t "
           "JOIN items i ON i.id = t.item_id ")
    params = []
    if item_id is not None:
        sql += "WHERE t.item_id = ? "
        params.append(item_id)
    sql += "ORDER BY t.id DESC LIMIT ?"
    params.append(limit)
    with db.session() as conn:
        rows = [dict(r) for r in conn.execute(sql, params)]
    for row in rows:
        row["label"] = KIND_LABEL.get(row["kind"], row["kind"])
    return rows


def opening_balances(user=None):
    """Give pre-ledger items a starting lot so the two agree from day one.

    Only touches items that have no lots at all, so it is safe to run on
    every start and will not disturb anything already tracked.
    """
    with db.session() as conn:
        rows = conn.execute(
            "SELECT id, qty FROM items WHERE qty > 0 AND id NOT IN "
            "(SELECT DISTINCT item_id FROM lots)"
        ).fetchall()
        for row in rows:
            lot_id = conn.execute(
                "INSERT INTO lots (item_id, qty, note) VALUES (?, ?, 'opening balance')",
                (row["id"], row["qty"]),
            ).lastrowid
            _log(conn, row["id"], lot_id, row["qty"], ADJUSTMENT, user, "opening balance")
    return len(rows)
