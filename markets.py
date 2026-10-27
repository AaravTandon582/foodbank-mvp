"""Price sourcing.

Every market is behind a `PriceSource`. The MVP ships with a simulated source so
the app runs with no API keys, but the interface is the seam where a real
store API or scraper drops in: write a class with the same `fetch` signature,
register it in SOURCES, and nothing else in the app changes.
"""
import hashlib
from dataclasses import dataclass
from datetime import date

import db


@dataclass
class Quote:
    market_id: int
    market_name: str
    distance: float
    item_name: str
    price: float          # dollars per one unit of the item
    source: str


# Rough wholesale-ish starting points, per unit, for staples a food bank buys.
BASE_PRICES = {
    "rice": 1.10, "beans": 1.25, "pasta": 1.05, "canned corn": 0.95,
    "canned tomatoes": 1.15, "canned tuna": 1.55, "peanut butter": 2.80,
    "cereal": 3.10, "flour": 0.85, "sugar": 0.95, "cooking oil": 4.20,
    "milk": 3.40, "eggs": 3.60, "bread": 2.10, "apples": 1.40,
    "potatoes": 0.80, "onions": 0.90, "carrots": 1.00, "chicken": 3.90,
    "diapers": 0.35, "soap": 1.20, "baby formula": 24.00,
}
FALLBACK_PRICE = 2.50

# Each market has a house multiplier: warehouse clubs run cheap, corner
# stores run dear. Anything not listed is treated as average.
HOUSE_MULTIPLIER = {
    "Costco Business Center": 0.82,
    "Restaurant Depot": 0.85,
    "WinCo Foods": 0.90,
    "Walmart Supercenter": 0.94,
    "Aldi": 0.92,
    "Safeway": 1.08,
    "Whole Foods Market": 1.30,
    "Corner Grocery": 1.22,
}


def _jitter(market_name, item_name):
    """Stable per-market, per-item, per-day price wobble in the range 0.88-1.12.

    md5 is used as a cheap deterministic hash, not for security — flagged as
    such so an audit tool does not report it as a weak digest.
    """
    seed = f"{market_name}|{item_name}|{date.today().isoformat()}"
    digest = hashlib.md5(seed.encode(), usedforsecurity=False).hexdigest()
    return 0.88 + (int(digest[:8], 16) % 1000) / 1000 * 0.24


class SimulatedSource:
    """Stand-in for a real market feed. Deterministic within a day."""

    name = "simulated"

    def fetch(self, item_names, markets):
        quotes = []
        for market in markets:
            house = HOUSE_MULTIPLIER.get(market["name"], 1.0)
            for item_name in item_names:
                base = BASE_PRICES.get(item_name.strip().lower(), FALLBACK_PRICE)
                price = base * house * _jitter(market["name"], item_name)
                quotes.append(
                    Quote(
                        market_id=market["id"],
                        market_name=market["name"],
                        distance=market["distance"],
                        item_name=item_name,
                        price=round(price, 2),
                        source=self.name,
                    )
                )
        return quotes


SOURCES = [SimulatedSource()]


def fill_demo_gaps(item_names):
    """Generate demo prices ONLY where no real price exists.

    A real price, once found, is never overwritten by a simulated one.
    """
    if not item_names:
        return 0

    with db.session() as conn:
        markets = [dict(m) for m in conn.execute(
            "SELECT id, name, distance FROM markets WHERE enabled = 1"
        )]
        real = {
            (r["market_id"], r["item_name"])
            for r in conn.execute(
                "SELECT market_id, item_name FROM prices WHERE source != 'simulated'"
            )
        }

        quotes = []
        for source in SOURCES:
            quotes.extend(source.fetch(item_names, markets))
        quotes = [q for q in quotes if (q.market_id, q.item_name) not in real]

        conn.executemany(
            "INSERT INTO prices (market_id, item_name, price, source) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(market_id, item_name) DO UPDATE SET "
            "price = excluded.price, source = excluded.source, "
            "checked_at = datetime('now')",
            [(q.market_id, q.item_name, q.price, q.source) for q in quotes],
        )
    return len(quotes)
