"""One refresh path, shared by the web button and the CLI.

Real sources run first. The simulator only fills gaps, so a demo price can
never overwrite a price someone actually went and found.
"""
import math

import db
import markets
import scrapers

# A price outside this range is a parsing accident, not a grocery price: a
# scraper that grabbed a phone number, a loyalty-card number, or a zip code.
MIN_PRICE = 0.01
MAX_PRICE = 5000.0


def _usable(obs):
    """Reject observations that would poison the shopping list."""
    try:
        price = float(obs["price"])
        item = str(obs["item"]).strip()
        store = str(obs["store"]).strip()
    except (KeyError, TypeError, ValueError):
        return False
    if not item or not store:
        return False
    return math.isfinite(price) and MIN_PRICE <= price <= MAX_PRICE


def ensure_markets(names, conn):
    """Map store names to ids, adding unknown stores disabled.

    A new store is added switched off so it cannot win the shopping list
    before someone has said how far away it is.
    """
    existing = {
        r["name"].lower(): r["id"]
        for r in conn.execute("SELECT id, name FROM markets")
    }
    ids, new = {}, []
    for name in dict.fromkeys(names):          # de-duplicate, keep order
        key = name.lower()
        if key in existing:
            ids[name] = existing[key]
            continue
        cur = conn.execute(
            "INSERT INTO markets (name, distance, enabled) VALUES (?, 0, 0)", (name,)
        )
        existing[key] = ids[name] = cur.lastrowid
        new.append(name)
    return ids, new


def refresh(item_names, use_demo=True, dry_run=False):
    """Pull from every real source, then top up with demo prices.

    Returns a report: per-source counts, new stores, rows written.
    """
    report = {"sources": {}, "new_stores": [], "written": 0,
              "demo_filled": 0, "observations": [], "rejected": 0}
    if not item_names:
        return report

    observations = []
    for source in scrapers.active_sources():
        try:
            found = source.observations(item_names)
        except Exception as exc:
            report["sources"][source.name] = f"failed: {type(exc).__name__}"
            continue
        good = [o for o in found if _usable(o)]
        report["rejected"] += len(found) - len(good)
        report["sources"][source.name] = len(good)
        observations.extend(good)

    report["observations"] = observations
    if dry_run:
        return report

    if observations:
        # One connection and two batched statements, rather than opening a
        # connection per observation inside the loop.
        with db.session() as conn:
            ids, new = ensure_markets([o["store"] for o in observations], conn)
            report["new_stores"] = sorted(set(new))
            conn.executemany(
                "INSERT INTO prices (market_id, item_name, price, source) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(market_id, item_name) DO UPDATE SET "
                "price = excluded.price, source = excluded.source, "
                "checked_at = datetime('now')",
                [(ids[o["store"]], o["item"], round(float(o["price"]), 2), o["source"])
                 for o in observations],
            )
            report["written"] = len(observations)

    if use_demo:
        report["demo_filled"] = markets.fill_demo_gaps(item_names)

    return report
