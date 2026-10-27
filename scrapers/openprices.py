"""Open Prices — crowdsourced grocery prices from the Open Food Facts project.

This one works right now, with no key and no scraping: it is a documented
public API over an openly licensed database, which is always the better deal
when it exists.

Coverage is the catch. It is contributed by volunteers, so fresh produce is
covered reasonably and packaged staples barely at all. Treat it as one input,
not the whole picture.

API: https://prices.openfoodfacts.org/api/v1/prices
"""
from collections import defaultdict
from datetime import date, timedelta

from .polite import PoliteFetcher

BASE = "https://prices.openfoodfacts.org/api/v1/prices"

# Our item names -> Open Food Facts category tags. Only raw produce resolves
# well; packaged goods need a barcode lookup instead, which is why they are
# absent here rather than wrong.
CATEGORY_TAGS = {
    "apples": "en:apples",
    "bananas": "en:bananas",
    "carrots": "en:carrots",
    "onions": "en:onions",
    "potatoes": "en:potatoes",
    "tomatoes": "en:tomatoes",
    "oranges": "en:oranges",
    "zucchini": "en:zucchini",
    "bell peppers": "en:red-bell-peppers",
}

KG_TO_LB = 0.453592


class OpenPricesSource:
    """Live prices by category, grouped to the store that reported them."""

    name = "openprices"

    def __init__(self, fetcher=None, currency="USD", max_age_days=120):
        self.fetch_client = fetcher or PoliteFetcher(delay=1.0)
        self.currency = currency
        self.max_age_days = max_age_days

    def _recent(self, tag, limit=50):
        """Most recent reported prices for one category tag."""
        data = self.fetch_client.get_json(BASE, params={
            "currency": self.currency,
            "category_tag": tag,
            "order_by": "-date",
            "size": limit,
        })
        cutoff = date.today() - timedelta(days=self.max_age_days)
        rows = []
        for item in data.get("items", []):
            try:
                seen = date.fromisoformat(item["date"])
            except (KeyError, TypeError, ValueError):
                continue
            if seen < cutoff:
                continue
            store = (item.get("location") or {}).get("osm_name")
            price = item.get("price")
            if not store or price is None:
                continue
            # Prices come per kg or per unit; normalise per-kg to per-lb.
            if (item.get("price_per") or "").upper() == "KILOGRAM":
                price = price * KG_TO_LB
            rows.append({"store": store, "price": float(price), "date": seen})
        return rows

    def observations(self, item_names):
        """Raw market observations: [{item, store, price, date}, ...].

        Returns the median per (item, store) so one odd report cannot swing it.
        """
        out = []
        for item_name in item_names:
            tag = CATEGORY_TAGS.get(item_name.strip().lower())
            if not tag:
                continue
            by_store = defaultdict(list)
            for row in self._recent(tag):
                by_store[row["store"]].append(row)

            for store, rows in by_store.items():
                prices = sorted(r["price"] for r in rows)
                median = prices[len(prices) // 2]
                out.append({
                    "item": item_name,
                    "store": store,
                    "price": round(median, 2),
                    "date": max(r["date"] for r in rows).isoformat(),
                    "samples": len(rows),
                    "source": self.name,
                })
        return out
