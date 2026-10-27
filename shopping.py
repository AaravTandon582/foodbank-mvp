"""Turn low stock + cached prices + preferences into a shopping plan."""
import math

import db

# SQLite caps host parameters per statement. Well under any real limit, and it
# keeps a single query from growing unbounded with the item list.
CHUNK = 400


def low_stock():
    """Items below their par level, with how much is needed to refill."""
    with db.session() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE qty < par_level ORDER BY "
            "(par_level - qty) / NULLIF(par_level, 0) DESC, name"
        ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["needed"] = math.ceil(item["par_level"] - item["qty"])
        item["pct"] = (item["qty"] / item["par_level"] * 100) if item["par_level"] else 100
        out.append(item)
    return out


def _candidates(item_names, settings, conn):
    """Cached prices for many items at once, keyed by item name.

    One query for the whole list. Asking per item meant a fresh connection and
    a full table scan for every low item on the page.
    """
    by_item = {name: [] for name in item_names}
    max_age = settings["price_max_age_days"]

    names = list(item_names)
    for start in range(0, len(names), CHUNK):
        batch = names[start:start + CHUNK]
        holes = ",".join("?" * len(batch))
        rows = conn.execute(
            "SELECT p.item_name, p.price, p.checked_at, p.source, "
            "       m.id AS market_id, m.name AS market, m.distance, "
            "       julianday('now') - julianday(p.checked_at) AS age_days "
            "FROM prices p JOIN markets m ON m.id = p.market_id "
            f"WHERE p.item_name IN ({holes}) AND m.enabled = 1 AND m.distance <= ? "
            "ORDER BY p.item_name, p.price",
            (*batch, settings["max_distance"]),
        ).fetchall()
        for row in rows:
            option = dict(row)
            age = option.pop("age_days") or 0
            option["age_days"] = round(age, 1)
            option["stale"] = age > max_age
            by_item[option["item_name"]].append(option)
    return by_item


def _score(option, cheapest_price, settings):
    """Lower is better. 1.0 means cheapest available and zero distance."""
    w = settings["price_weight"]
    price_score = option["price"] / cheapest_price if cheapest_price else 1.0
    dist_score = 1 + (option["distance"] / settings["max_distance"]
                      if settings["max_distance"] else 0)
    return w * price_score + (1 - w) * dist_score


def build_plan():
    """The shopping list: one pick per low item, plus a per-market run sheet."""
    settings = db.get_settings()
    items = low_stock()
    lines, unpriced = [], []

    with db.session() as conn:
        candidates = _candidates([i["name"] for i in items], settings, conn)

    for item in items:
        options = candidates.get(item["name"]) or []
        if not options:
            unpriced.append(item)
            continue

        # `options` arrives sorted by price, so the ends are the extremes.
        cheapest = options[0]["price"]
        for o in options:
            o["score"] = _score(o, cheapest, settings)
        pick = min(options, key=lambda o: o["score"])
        worst = options[-1]

        # The runner-up list must exclude the pick itself. Slicing the
        # price-sorted list blindly showed the chosen market as its own
        # alternative whenever distance weighting moved the pick off the
        # cheapest row — which was most of them.
        alternatives = [o for o in options if o["market_id"] != pick["market_id"]][:3]

        lines.append({
            "item": item["name"],
            "category": item["category"],
            "unit": item["unit"],
            "needed": item["needed"],
            "market": pick["market"],
            "distance": pick["distance"],
            "price": pick["price"],
            "cost": round(pick["price"] * item["needed"], 2),
            "checked_at": pick["checked_at"],
            "source": pick["source"],
            "stale": pick["stale"],
            "age_days": pick["age_days"],
            # What this pick saves against the priciest option in range.
            "saved": round((worst["price"] - pick["price"]) * item["needed"], 2),
            "alternatives": alternatives,
            "options": options,
        })

    # Group into one stop per market so staff can actually run the trip.
    stops = {}
    for line in lines:
        stop = stops.setdefault(line["market"], {
            "market": line["market"], "distance": line["distance"],
            "lines": [], "subtotal": 0.0,
        })
        stop["lines"].append(line)
        stop["subtotal"] = round(stop["subtotal"] + line["cost"], 2)

    stops = sorted(stops.values(), key=lambda s: s["distance"])
    driving = round(
        sum(s["distance"] * 2 for s in stops) * settings["cost_per_mile"], 2
    )
    subtotal = round(sum(line["cost"] for line in lines), 2)

    return {
        "lines": lines,
        "stops": stops,
        "unpriced": unpriced,
        "subtotal": subtotal,
        "driving": driving,
        "total": round(subtotal + driving, 2),
        "saved": round(sum(line["saved"] for line in lines), 2),
        "stale_count": sum(1 for line in lines if line["stale"]),
        "settings": settings,
    }
