"""SQLite storage for the food bank MVP. Standard library only.

Two rules hold this file together:

  * Every connection is opened through `session()`, which commits on success,
    rolls back on an exception, and **always closes**. `sqlite3`'s own
    connection context manager commits but does not close, so `with
    connect() as conn` leaks a file handle per request.
  * Every column the app filters on has an index. Without them the shopping
    list full-scans `prices` once per low item.
"""
import math
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.getenv("FOODBANK_DB") or Path(__file__).parent / "foodbank.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    category    TEXT NOT NULL DEFAULT 'Other',
    unit        TEXT NOT NULL DEFAULT 'each',
    qty         REAL NOT NULL DEFAULT 0,
    par_level   REAL NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS markets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    distance    REAL NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 1
);

-- Cached prices. One row per (market, item), price is per `unit` of the item.
CREATE TABLE IF NOT EXISTS prices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id   INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    item_name   TEXT NOT NULL,
    price       REAL NOT NULL,
    source      TEXT NOT NULL DEFAULT 'simulated',
    checked_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (market_id, item_name)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Who is allowed in. `role` is 'admin' or 'volunteer'; see auth.py.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'volunteer',
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_login    TEXT
);

-- A physical batch of one item, with the date it goes off. Expiry lives here
-- rather than on `items` because one item is many batches with different
-- dates, which is the whole reason rotation is a problem worth solving.
-- A NULL `expires_on` means non-perishable.
CREATE TABLE IF NOT EXISTS lots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    qty         REAL NOT NULL DEFAULT 0,
    expires_on  TEXT,
    received_on TEXT NOT NULL DEFAULT (date('now')),
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Every quantity change, ever. `items.qty` is a cached total derived from
-- this ledger; nothing may write it directly except inventory.adjust().
-- `username` is denormalised on purpose: the history has to stay readable
-- after a volunteer's account is removed.
CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    lot_id      INTEGER REFERENCES lots(id) ON DELETE SET NULL,
    delta       REAL NOT NULL,
    kind        TEXT NOT NULL,
    user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    username    TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- FEFO reads lots for one item in expiry order; the expiry dashboard reads
-- every open lot in expiry order.
CREATE INDEX IF NOT EXISTS idx_lots_item ON lots (item_id, expires_on);
CREATE INDEX IF NOT EXISTS idx_lots_expiry ON lots (expires_on) WHERE qty > 0;

-- History is always read newest-first, per item or across the board.
CREATE INDEX IF NOT EXISTS idx_tx_item ON transactions (item_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_tx_recent ON transactions (id DESC);

-- The shopping list looks prices up by item name. The UNIQUE constraint above
-- indexes (market_id, item_name), whose leading column is the wrong one, so
-- that lookup would scan the whole table. This covers it, price included so
-- the common query is answered from the index alone.
CREATE INDEX IF NOT EXISTS idx_prices_item ON prices (item_name, market_id, price);

-- Market filtering on the same query.
CREATE INDEX IF NOT EXISTS idx_markets_enabled ON markets (enabled, distance);

-- The inventory page orders by these.
CREATE INDEX IF NOT EXISTS idx_items_category ON items (category, name);
"""

DEFAULT_SETTINGS = {
    # 0 = only care about distance, 1 = only care about price.
    "price_weight": "0.75",
    # Ignore markets further away than this (miles).
    "max_distance": "15",
    # Cost of driving one mile, used to turn distance into dollars.
    "cost_per_mile": "0.70",
    # A cached price older than this is flagged as stale on the shopping list.
    "price_max_age_days": "14",
}

# Every setting is clamped to a sane range on the way out, so a hand-edited
# database or a stale row cannot push the scorer into nonsense.
SETTING_BOUNDS = {
    "price_weight": (0.0, 1.0),
    "max_distance": (0.0, 500.0),
    "cost_per_mile": (0.0, 20.0),
    "price_max_age_days": (1.0, 3650.0),
}


def connect():
    """A raw connection. Prefer `session()` — it closes."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def session():
    """Commit on success, roll back on error, close either way."""
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init():
    with session() as conn:
        # WAL lets the page you are reading survive a concurrent write, and is
        # stored in the file itself, so setting it once here is enough.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # Migration for databases created before prices carried provenance.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(prices)")}
        if "source" not in cols:
            conn.execute(
                "ALTER TABLE prices ADD COLUMN source TEXT NOT NULL DEFAULT 'simulated'"
            )
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )
        # Without table statistics the planner falls back to guesswork and
        # ignores the covering index above. `optimize` re-analyses only what
        # has gone stale, so this is cheap to run on every start.
        conn.execute("PRAGMA optimize")


def _clean(key, raw, fallback):
    """A setting value that is always a finite float inside its bounds."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float(fallback)
    if not math.isfinite(value):
        value = float(fallback)
    low, high = SETTING_BOUNDS.get(key, (-math.inf, math.inf))
    return min(max(value, low), high)


def get_settings():
    with session() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    stored = {r["key"]: r["value"] for r in rows}
    return {
        key: _clean(key, stored.get(key, default), default)
        for key, default in DEFAULT_SETTINGS.items()
    }


def save_settings(values):
    """Persist settings, clamped. Unknown keys are ignored."""
    clean = {
        key: _clean(key, value, DEFAULT_SETTINGS[key])
        for key, value in values.items()
        if key in DEFAULT_SETTINGS
    }
    with session() as conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, str(v)) for k, v in clean.items()],
        )
    return clean
