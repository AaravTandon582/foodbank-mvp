"""Price sources, best-first.

Order matters: an official API beats an open dataset, which beats scraping,
which beats nothing. Scraping is the last resort, not the first move.
"""
import os
from pathlib import Path

from .kroger import KrogerSource
from .manual import ManualSource
from .openprices import OpenPricesSource
from .polite import PoliteFetcher, RobotsBlocked
from .storefront import StorefrontSource

PRICES_CSV = Path(__file__).resolve().parent.parent / "prices.csv"

# Add your own local stores here. Check robots.txt first — the fetcher will
# refuse a disallowed URL anyway, but knowing beforehand saves you the work.
STOREFRONTS = []


def active_sources():
    """Every source that is actually usable right now."""
    sources = [OpenPricesSource(), ManualSource(PRICES_CSV)]

    kroger = KrogerSource(zipcode=os.getenv("FOODBANK_ZIP", "97205"))
    if kroger.configured:
        sources.append(kroger)

    fetcher = PoliteFetcher(delay=3.0)
    sources.extend(StorefrontSource(r, fetcher=fetcher) for r in STOREFRONTS)
    return sources


__all__ = [
    "active_sources", "KrogerSource", "ManualSource", "OpenPricesSource",
    "StorefrontSource", "PoliteFetcher", "RobotsBlocked", "PRICES_CSV",
]
