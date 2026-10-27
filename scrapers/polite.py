"""The fetch engine every scraper sits on top of.

Scraping responsibly is mostly this file. It does four things that keep you
from getting blocked (or sued):

  1. Reads the site's robots.txt and REFUSES disallowed URLs. Hard refusal,
     not a warning you can ignore.
  2. Waits between requests to the same domain, honouring Crawl-delay.
  3. Caches to disk, so a rerun costs zero requests.
  4. Backs off on 429/5xx instead of hammering.

It identifies itself honestly in the User-Agent. Do not change that to
impersonate a browser: if a site does not want to be scraped, the answer is to
stop, not to disguise the request.
"""
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

try:
    # Scrapy's parser: understands * and $ wildcards, which the standard
    # library's robotparser silently ignores (it would wave through a
    # "Disallow: /mylist/*" rule).
    from protego import Protego
except ImportError:
    Protego = None
    import urllib.robotparser

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"
USER_AGENT = (
    "PantryOS-FoodBank/0.1 (inventory restocking for a food bank; "
    "contact: you@yourfoodbank.org)"
)


class RobotsBlocked(Exception):
    """The site's robots.txt disallows this URL. Do not work around this."""


class PoliteFetcher:
    def __init__(self, delay=2.0, cache_ttl=6 * 3600, user_agent=USER_AGENT,
                 obey_robots=True):
        self.delay = delay              # seconds between hits on one domain
        self.cache_ttl = cache_ttl      # how long a cached page stays fresh
        self.user_agent = user_agent
        self.obey_robots = obey_robots
        self._robots = {}               # domain -> RobotFileParser
        self._last_hit = {}             # domain -> timestamp
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        CACHE_DIR.mkdir(exist_ok=True)

    # ---------------------------------------------------------------- robots

    def _robots_for(self, url):
        parts = urlparse(url)
        domain = parts.netloc
        if domain in self._robots:
            return self._robots[domain]

        robots_url = f"{parts.scheme}://{domain}/robots.txt"
        rp = None
        try:
            if Protego is not None:
                resp = self.session.get(robots_url, timeout=15)
                # No robots.txt at all means no restrictions.
                text = resp.text if resp.status_code == 200 else ""
                rp = Protego.parse(text)
            else:
                rp = urllib.robotparser.RobotFileParser()
                rp.set_url(robots_url)
                rp.read()
        except Exception:
            # Unreachable robots.txt: assume closed, not open.
            rp = None
        self._robots[domain] = rp
        return rp

    def allowed(self, url):
        if not self.obey_robots:
            return True
        rp = self._robots_for(url)
        if rp is None:
            return False
        if Protego is not None:
            return rp.can_fetch(url, self.user_agent)
        return rp.can_fetch(self.user_agent, url)

    def _crawl_delay(self, url):
        rp = self._robots_for(url)
        if rp is None:
            return self.delay
        try:
            declared = rp.crawl_delay(self.user_agent)
        except Exception:
            declared = None
        return max(self.delay, float(declared or 0))

    # ----------------------------------------------------------------- cache

    def _cache_path(self, url):
        return CACHE_DIR / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".json")

    def _cached(self, url):
        path = self._cache_path(url)
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > self.cache_ttl:
            return None
        try:
            return json.loads(path.read_text())["body"]
        except Exception:
            return None

    def _store(self, url, body):
        self._cache_path(url).write_text(json.dumps({"url": url, "body": body}))

    # ----------------------------------------------------------------- fetch

    def get(self, url, params=None, headers=None, tries=3):
        """Fetch a URL politely. Returns the body as text, or raises."""
        full = requests.Request("GET", url, params=params).prepare().url

        hit = self._cached(full)
        if hit is not None:
            return hit

        if not self.allowed(full):
            raise RobotsBlocked(f"robots.txt disallows {full}")

        domain = urlparse(full).netloc
        wait = self._crawl_delay(full) - (time.time() - self._last_hit.get(domain, 0))
        if wait > 0:
            time.sleep(wait)

        backoff = 2.0
        for attempt in range(tries):
            self._last_hit[domain] = time.time()
            resp = self.session.get(full, headers=headers or {}, timeout=20)

            if resp.status_code == 200:
                self._store(full, resp.text)
                return resp.text

            # Too many requests or server trouble: wait and retry.
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                pause = float(resp.headers.get("Retry-After") or backoff)
                time.sleep(pause)
                backoff *= 2
                continue

            # 403 here usually means bot protection. That is a "no".
            resp.raise_for_status()

        raise RuntimeError(f"gave up on {full}")

    def get_json(self, url, params=None, headers=None):
        return json.loads(self.get(url, params=params, headers=headers))
