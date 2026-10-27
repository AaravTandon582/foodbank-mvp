"""A configurable HTML scraper for stores with no API.

This is scraping proper: fetch the search page, find the price in the markup,
pull out a number. Every store needs its own recipe because every store's HTML
differs, so a store is described by config rather than by a new class.

Two things do the real work:

  * PoliteFetcher refuses anything robots.txt disallows, so a misconfigured
    recipe fails loudly instead of quietly misbehaving.
  * Prices often live in JSON-LD (<script type="application/ld+json">) rather
    than in visible markup. Check there first — it is structured, stable, and
    far less brittle than CSS selectors against a redesign.

Worth knowing before you invest here: the big chains are mostly closed. Aldi's
robots.txt is "User-Agent: * / Disallow: /", Walmart disallows /api/, and both
sit behind bot protection that a plain request will not pass. Small local
grocers, co-ops and ethnic markets are where this actually pays off — and they
are often the cheaper source anyway.
"""
import json
import re

from bs4 import BeautifulSoup

from .polite import PoliteFetcher, RobotsBlocked

PRICE_RE = re.compile(r"\$?\s*(\d+(?:[.,]\d{1,2})?)")


def parse_price(text):
    """'$3.49 /lb' -> 3.49. Returns None when there is no number."""
    if text is None:
        return None
    match = PRICE_RE.search(str(text).replace(",", ""))
    return float(match.group(1)) if match else None


def from_json_ld(soup):
    """Prices declared in schema.org JSON-LD. Prefer this over selectors."""
    found = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            blob = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = blob if isinstance(blob, list) else [blob]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            if node.get("@type") in ("Product", "Offer"):
                offers = node.get("offers") or node
                offers = offers if isinstance(offers, list) else [offers]
                for offer in offers:
                    if isinstance(offer, dict) and offer.get("price"):
                        found.append({
                            "name": node.get("name"),
                            "price": parse_price(offer["price"]),
                        })
    return [f for f in found if f["price"]]


class StorefrontSource:
    """One store, described by a recipe.

    recipe = {
        "store":       "Sunrise Market",
        "search_url":  "https://example.com/search?q={query}",
        "item":        "div.product-card",   # optional: repeated result block
        "name":        "h3.title",           # optional: name within the block
        "price":       "span.price",         # price within the block
    }
    """

    def __init__(self, recipe, fetcher=None):
        self.recipe = recipe
        self.name = f"storefront:{recipe['store']}"
        self.fetch_client = fetcher or PoliteFetcher(delay=3.0)

    def _search(self, term):
        url = self.recipe["search_url"].format(query=term.replace(" ", "+"))
        html = self.fetch_client.get(url)
        soup = BeautifulSoup(html, "lxml")

        # 1. Structured data first — most reliable.
        hits = [h["price"] for h in from_json_ld(soup)]
        if hits:
            return hits

        # 2. Fall back to CSS selectors on the rendered markup.
        blocks = soup.select(self.recipe["item"]) if self.recipe.get("item") else [soup]
        prices = []
        for block in blocks:
            node = block.select_one(self.recipe["price"])
            price = parse_price(node.get_text() if node else None)
            if price:
                prices.append(price)
        return prices

    def observations(self, item_names):
        out = []
        for item_name in item_names:
            try:
                prices = self._search(item_name)
            except RobotsBlocked:
                # The store told us not to. Stop, do not work around it.
                break
            except Exception:
                continue
            if prices:
                out.append({
                    "item": item_name,
                    "store": self.recipe["store"],
                    "price": round(min(prices), 2),
                    "samples": len(prices),
                    "source": self.name,
                })
        return out
