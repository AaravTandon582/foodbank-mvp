"""Connection lifecycle and the indexes the shopping list depends on."""
import sqlite3

import pytest

import db


def test_session_closes_the_connection(fresh_db):
    """`with connect()` commits but never closes — that leaked a handle a request."""
    captured = {}
    with db.session() as conn:
        captured["conn"] = conn
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        captured["conn"].execute("SELECT 1")


def test_session_commits_on_success(fresh_db):
    with db.session() as conn:
        conn.execute("INSERT INTO items (name, qty, par_level) VALUES ('Quinoa', 1, 2)")
    with db.session() as conn:
        assert conn.execute("SELECT 1 FROM items WHERE name='Quinoa'").fetchone()


def test_session_rolls_back_on_error(fresh_db):
    with pytest.raises(RuntimeError):
        with db.session() as conn:
            conn.execute("INSERT INTO items (name, qty, par_level) VALUES ('Ghost', 1, 2)")
            raise RuntimeError("boom")
    with db.session() as conn:
        assert conn.execute("SELECT 1 FROM items WHERE name='Ghost'").fetchone() is None


def test_price_lookup_never_scans_a_whole_table(fresh_db):
    """Originally this plan was `SCAN p` — a full pass over prices per item."""
    with db.session() as conn:
        plan = [r[3] for r in conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT p.price FROM prices p JOIN markets m ON m.id = p.market_id "
            "WHERE p.item_name IN ('Rice') AND m.enabled = 1 AND m.distance <= 15"
        )]
    assert not any(step.startswith("SCAN") for step in plan), plan
    assert all("USING" in step and "INDEX" in step for step in plan), plan


def test_the_covering_index_is_used_once_stats_exist(fresh_db):
    """On a seed-sized table SQLite drives from `markets`; the item index only
    wins once ANALYZE has run, which `PRAGMA optimize` does as data grows.
    This pins that it is a real index and not dead weight."""
    with db.session() as conn:
        conn.execute("ANALYZE")
        plan = [r[3] for r in conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT p.price FROM prices p JOIN markets m ON m.id = p.market_id "
            "WHERE p.item_name IN ('Rice') AND m.enabled = 1 AND m.distance <= 15"
        )]
    assert any("idx_prices_item" in step for step in plan), plan


def test_foreign_keys_cascade(fresh_db):
    with db.session() as conn:
        mid = conn.execute("SELECT id FROM markets LIMIT 1").fetchone()["id"]
        before = conn.execute(
            "SELECT COUNT(*) c FROM prices WHERE market_id = ?", (mid,)).fetchone()["c"]
        assert before > 0
        conn.execute("DELETE FROM markets WHERE id = ?", (mid,))
        after = conn.execute(
            "SELECT COUNT(*) c FROM prices WHERE market_id = ?", (mid,)).fetchone()["c"]
    assert after == 0, "orphaned price rows: ON DELETE CASCADE is not firing"


def test_wal_is_enabled(fresh_db):
    with db.session() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
