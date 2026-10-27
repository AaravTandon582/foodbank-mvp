"""The shopping plan: the picks, the arithmetic, and the N+1 that was in it."""
import db
import shopping


def test_pick_is_never_listed_as_its_own_alternative(client):
    """This was visible on 14 of 16 rows on the demo data."""
    plan = shopping.build_plan()
    assert plan["lines"], "expected the seeded data to have low items"
    for line in plan["lines"]:
        markets = [a["market"] for a in line["alternatives"]]
        assert line["market"] not in markets, line["item"]


def test_alternatives_are_cheapest_first_and_capped(client):
    for line in shopping.build_plan()["lines"]:
        prices = [a["price"] for a in line["alternatives"]]
        assert prices == sorted(prices)
        assert len(prices) <= 3


def test_price_weight_changes_the_pick(client):
    """All-price should never pick a dearer option than all-distance does."""
    db.save_settings({"price_weight": 1.0})
    cheap = {line["item"]: line["price"] for line in shopping.build_plan()["lines"]}
    db.save_settings({"price_weight": 0.0})
    near = {line["item"]: line["price"] for line in shopping.build_plan()["lines"]}
    assert all(cheap[k] <= near[k] + 1e-9 for k in cheap)


def test_totals_add_up(client):
    plan = shopping.build_plan()
    assert round(sum(line["cost"] for line in plan["lines"]), 2) == plan["subtotal"]
    assert round(plan["subtotal"] + plan["driving"], 2) == plan["total"]
    for stop in plan["stops"]:
        assert round(sum(line["cost"] for line in stop["lines"]), 2) == stop["subtotal"]


def test_disabled_and_distant_markets_are_excluded(client):
    db.save_settings({"max_distance": 3.0})
    with db.session() as conn:
        conn.execute("UPDATE markets SET enabled = 0 WHERE name = 'Aldi'")
    for line in shopping.build_plan()["lines"]:
        assert line["distance"] <= 3.0
        assert line["market"] != "Aldi"
        for alt in line["alternatives"]:
            assert alt["market"] != "Aldi"


def test_stale_prices_are_flagged(client):
    with db.session() as conn:
        conn.execute("UPDATE prices SET checked_at = datetime('now', '-90 days')")
    plan = shopping.build_plan()
    assert plan["stale_count"] == len(plan["lines"]) > 0
    assert all(line["stale"] for line in plan["lines"])


def test_build_plan_uses_one_query_per_page_not_one_per_item(client):
    """The regression guard for the N+1: cost must not scale with item count."""
    seen = []
    original = db.connect

    def counting_connect():
        conn = original()
        conn.set_trace_callback(seen.append)
        return conn

    db.connect = counting_connect
    try:
        plan = shopping.build_plan()
    finally:
        db.connect = original

    selects = [q for q in seen if q.strip().upper().startswith("SELECT")]
    assert len(plan["lines"]) >= 10, "need a decent item count for this to mean anything"
    # settings + low_stock + one batched price query.
    assert len(selects) <= 4, f"{len(selects)} SELECTs for {len(plan['lines'])} items: {selects}"


def test_items_with_no_price_land_in_unpriced(client):
    with db.session() as conn:
        conn.execute("INSERT INTO items (name, qty, par_level) VALUES ('Saffron', 0, 10)")
    plan = shopping.build_plan()
    assert "Saffron" in [i["name"] for i in plan["unpriced"]]
