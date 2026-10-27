"""Prices typed in or imported from a spreadsheet.

Unglamorous and the most reliable source you have. A volunteer with a phone in
an aisle beats every scraper for the stores that block bots, and a food bank
buying from a wholesaler is working off a quoted price list anyway, not a
website.

CSV columns: item,store,price[,date]
"""
import csv
from datetime import date
from pathlib import Path


class ManualSource:
    name = "manual"

    def __init__(self, path):
        self.path = Path(path)

    def observations(self, item_names):
        if not self.path.exists():
            return []
        wanted = {n.strip().lower() for n in item_names}
        out = []
        with self.path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                item = (row.get("item") or "").strip()
                if item.lower() not in wanted:
                    continue
                try:
                    price = float((row.get("price") or "").replace("$", "").strip())
                except ValueError:
                    continue
                out.append({
                    "item": item,
                    "store": (row.get("store") or "").strip(),
                    "price": round(price, 2),
                    "date": (row.get("date") or date.today().isoformat()).strip(),
                    "samples": 1,
                    "source": self.name,
                })
        return out
