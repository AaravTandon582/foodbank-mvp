"""Real prices beat demo prices, and implausible ones never land."""
import db
import markets
import refresh


class FakeSource:
    name = "fake"

    def __init__(self, rows):
        self.rows = rows

    def observations(self, item_names):
        return self.rows


class BrokenSource:
    name = "broken"

    def observations(self, item_names):
        raise ConnectionError("network down")


def _with_sources(monkeypatch, sources):
    monkeypatch.setattr("scrapers.active_sources", lambda: sources)


def test_implausible_prices_are_discarded(fresh_db, monkeypatch):
    _with_sources(monkeypatch, [FakeSource([
        {"item": "Rice", "store": "Good Store", "price": 1.20, "source": "fake"},
        {"item": "Rice", "store": "Bad Store", "price": 97205, "source": "fake"},
        {"item": "Rice", "store": "Bad Store", "price": float("nan"), "source": "fake"},
        {"item": "Rice", "store": "", "price": 1.0, "source": "fake"},
    ])])
    report = refresh.refresh(["Rice"], use_demo=False)
    assert report["written"] == 1
    assert report["rejected"] == 3
    with db.session() as conn:
        assert conn.execute(
            "SELECT 1 FROM markets WHERE name = 'Bad Store'").fetchone() is None


def test_a_failing_source_does_not_stop_the_others(fresh_db, monkeypatch):
    _with_sources(monkeypatch, [
        BrokenSource(),
        FakeSource([{"item": "Rice", "store": "Good Store", "price": 1.20, "source": "fake"}]),
    ])
    report = refresh.refresh(["Rice"], use_demo=False)
    assert report["written"] == 1
    assert "failed" in report["sources"]["broken"]


def test_new_stores_arrive_disabled(fresh_db, monkeypatch):
    _with_sources(monkeypatch, [FakeSource([
        {"item": "Rice", "store": "Brand New Co-op", "price": 0.99, "source": "fake"},
    ])])
    refresh.refresh(["Rice"], use_demo=False)
    with db.session() as conn:
        row = conn.execute(
            "SELECT enabled, distance FROM markets WHERE name = 'Brand New Co-op'"
        ).fetchone()
    assert row["enabled"] == 0, "a new store must not win the list before it has a distance"


def test_demo_never_overwrites_a_real_price(fresh_db, monkeypatch):
    _with_sources(monkeypatch, [FakeSource([
        {"item": "Rice", "store": "Aldi", "price": 0.55, "source": "fake"},
    ])])
    refresh.refresh(["Rice"], use_demo=True)
    markets.fill_demo_gaps(["Rice"])
    with db.session() as conn:
        row = conn.execute(
            "SELECT p.price, p.source FROM prices p JOIN markets m ON m.id = p.market_id "
            "WHERE m.name = 'Aldi' AND p.item_name = 'Rice'").fetchone()
    assert row["source"] == "fake" and row["price"] == 0.55


def test_store_names_are_matched_case_insensitively(fresh_db, monkeypatch):
    _with_sources(monkeypatch, [FakeSource([
        {"item": "Rice", "store": "aldi", "price": 0.77, "source": "fake"},
    ])])
    report = refresh.refresh(["Rice"], use_demo=False)
    assert report["new_stores"] == [], "created a duplicate market differing only in case"


def test_empty_item_list_is_a_no_op(fresh_db):
    assert refresh.refresh([])["written"] == 0
    assert markets.fill_demo_gaps([]) == 0
