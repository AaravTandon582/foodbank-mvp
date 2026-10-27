"""Kroger's official developer API — the best real source for US shelf prices.

Not a scraper: a documented API with per-store pricing, which is why it is the
first thing to try. Kroger also owns Fred Meyer, Ralphs, King Soopers, Smith's,
QFC, Fry's, Dillons and Harris Teeter, so one key covers a lot of the country.

Setup (free, ~5 minutes):
  1. Register at https://developer.kroger.com
  2. Create an app, scope "product.compact"
  3. export KROGER_CLIENT_ID=...  KROGER_CLIENT_SECRET=...

Rate limit is roughly 10k calls/day per endpoint, which is far more than a
food bank needs.
"""
import base64
import os
import time

import requests

from .polite import USER_AGENT

TOKEN_URL = "https://api.kroger.com/v1/connect/oauth2/token"
API = "https://api.kroger.com/v1"


class KrogerSource:
    name = "kroger"

    def __init__(self, client_id=None, client_secret=None, zipcode="97205",
                 radius=15, max_stores=4):
        self.client_id = client_id or os.getenv("KROGER_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("KROGER_CLIENT_SECRET")
        self.zipcode = zipcode
        self.radius = radius
        self.max_stores = max_stores
        self._token = None
        self._expires = 0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    @property
    def configured(self):
        return bool(self.client_id and self.client_secret)

    def _auth(self):
        """Client-credentials token, refreshed a minute before it lapses."""
        if self._token and time.time() < self._expires - 60:
            return self._token
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        resp = self.session.post(
            TOKEN_URL,
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": "product.compact"},
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._expires = time.time() + payload.get("expires_in", 1800)
        return self._token

    def _get(self, path, params):
        resp = self.session.get(
            f"{API}{path}",
            headers={"Authorization": f"Bearer {self._auth()}"},
            params=params, timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    def stores(self):
        """Nearby Kroger-family stores, closest first."""
        data = self._get("/locations", {
            "filter.zipCode.near": self.zipcode,
            "filter.radiusInMiles": self.radius,
            "filter.limit": self.max_stores,
        })
        return [{
            "id": loc["locationId"],
            "name": f"{loc.get('chain', 'Kroger').title()} — {loc['address']['city']}",
            "zip": loc["address"]["zipCode"],
        } for loc in data.get("data", [])]

    def observations(self, item_names):
        """Cheapest matching product per item, per nearby store."""
        if not self.configured:
            return []

        out = []
        for store in self.stores():
            for item_name in item_names:
                try:
                    data = self._get("/products", {
                        "filter.term": item_name,
                        "filter.locationId": store["id"],
                        "filter.limit": 12,
                    })
                except requests.HTTPError:
                    continue

                prices = []
                for product in data.get("data", []):
                    for unit in product.get("items", []):
                        price = unit.get("price") or {}
                        # Promo price is 0 when there is no promotion on.
                        best = min(
                            [p for p in (price.get("regular"), price.get("promo")) if p],
                            default=None,
                        )
                        if best:
                            prices.append(best)
                if prices:
                    out.append({
                        "item": item_name,
                        "store": store["name"],
                        "price": round(min(prices), 2),
                        "samples": len(prices),
                        "source": self.name,
                    })
        return out
