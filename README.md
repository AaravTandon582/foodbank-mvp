# PantryOS — food bank inventory + price finder (MVP)

Track what's in the pantry, see what's running low, and get a shopping list that
picks the cheapest nearby market for each item.

## Run it

```bash
cd ~/foodbank-mvp
python3 app.py
```

Open http://127.0.0.1:5000. The app itself needs only Flask and SQLite and no
API keys. First run seeds 22 staple items and 8 local markets so there's
something to look at; delete `foodbank.db` to start over.

The real price sources in `scrapers/` need a few libraries. Without them the
app still runs — it just falls back to demo prices.

```bash
pip install -r requirements.txt
python3 -m pytest          # 73 tests, ~2s
```

It binds to localhost. To run it on a network, put it behind a real WSGI
server and use `create_app()`, not `python3 app.py`:

```bash
FOODBANK_SECRET_KEY=$(python3 -c "import secrets;print(secrets.token_hex(32))") \
FOODBANK_HTTPS=1 gunicorn "app:create_app()"
```

On first run it prints a generated administrator password once. Sign in with
it and change it on the Accounts page. Set `FOODBANK_ADMIN_PASSWORD` to choose
it yourself instead.

## The screens

**Inventory** — every item with its on-hand count and its *par level* (the
minimum you want on the shelf). Anything under par is highlighted, with +/- and
+10 buttons for quick counts. Click an item for its lots and its history.

**Item** — record intake (a donation arriving, with the date on the packaging)
and distribution (stock going out). Shows the lots on hand in the order they
should leave the shelf, and every movement ever recorded against the item.

**Expiring** — dated lots within the next 30 days, soonest first, with anything
already past its date called out. This is the hand-out order.

**Shopping list** — for each low item it works out how much you need, compares
every market in range, and picks one. Results are grouped into one stop per
market so staff can actually run the trip, and each row shows the next-cheapest
alternatives so a pick can be overridden. Totals include an estimated driving
cost.

**Markets** — add or remove stores, set how far each one is, skip ones you don't
use, and set the preference slider. Administrators only.

**History** — every stock movement, newest first: who, what, when and why.

**Accounts** — who can sign in, and at what level. Administrators only.

## How a pick is made

For each item, across enabled markets inside your distance limit:

```
score = price_weight × (price ÷ cheapest price)
      + (1 − price_weight) × (1 + distance ÷ max distance)
```

Lowest score wins. Slide all the way to price and it will drive 12 miles to save
a dollar; slide toward distance and it keeps trips short. Both totals are shown
so the trade-off is visible.

## Real prices

```bash
python3 fetch_prices.py --dry-run   # see what would be written
python3 fetch_prices.py             # write prices for low items
python3 fetch_prices.py --all       # price everything
python3 fetch_prices.py --no-demo   # real prices only, no demo gap-filling
```

Every price in the app is tagged with where it came from, and the shopping list
badges it: orange **demo** for the built-in simulator, green for a real source.
You should never be guessing which is which.

Age is badged too. A price older than the age limit on the Markets page
(14 days by default) is marked **stale**, and the shopping list warns before
you trust a total built on one.

Real sources always run first and demo prices only fill the gaps, so a price
someone actually went and found is never overwritten by a simulated one.

Sources live in `scrapers/` and are tried best-first:

| Source | Status | Notes |
| --- | --- | --- |
| `openprices.py` | **working now** | Open Food Facts' open price database. No key. Good on fresh produce, thin on packaged goods. |
| `kroger.py` | needs a free key | Official API, real per-store shelf prices. Covers Fred Meyer, Ralphs, King Soopers, QFC, Smith's, Fry's, Harris Teeter. |
| `manual.py` | **working now** | `prices.csv`. A volunteer in an aisle, or a wholesaler's quote sheet. |
| `storefront.py` | per store | HTML scraping, for local stores with no API. |

A source is any object with `observations(item_names)` returning dicts of
`{item, store, price, source}`. Add it to `active_sources()` in
`scrapers/__init__.py` and the rest of the app just works. Stores that appear
in the data but aren't in your markets list get added **disabled**, so a store
can't win the shopping list before you've set how far away it is.

### Kroger setup

1. Register at https://developer.kroger.com (free)
2. Create an app with the `product.compact` scope
3. `export KROGER_CLIENT_ID=... KROGER_CLIENT_SECRET=... FOODBANK_ZIP=97205`

### Writing a scraper

`scrapers/polite.py` is the engine, and it is most of the job:

- **Reads robots.txt and refuses disallowed URLs.** Uses `protego`, so `*` and
  `$` wildcards actually work — the standard library's parser ignores them and
  would wave through a `Disallow: /mylist/*` rule.
- **Rate limits per domain**, honouring `Crawl-delay`.
- **Caches to disk**, so reruns cost zero requests.
- **Backs off** on 429/5xx instead of hammering.
- **Identifies itself honestly.** Don't change the User-Agent to impersonate a
  browser. If a site doesn't want to be scraped, the answer is to stop.

To add a local store, put a recipe in `STOREFRONTS` in `scrapers/__init__.py`:

```python
{
  "store": "Sunrise Market",
  "search_url": "https://example.com/search?q={query}",
  "item": "div.product-card",
  "price": "span.price",
}
```

`StorefrontSource` checks JSON-LD (`<script type="application/ld+json">`) before
falling back to your CSS selectors. Prefer it — it's structured data that
survives redesigns, where selectors break.

### What you can and can't scrape

Checked directly against their live robots.txt:

| Site | Rule | Verdict |
| --- | --- | --- |
| Aldi (Instacart-run) | `User-Agent: * / Disallow: /` | Off limits |
| Walmart | `Disallow: /api/` | No internal APIs; product pages behind bot protection |
| Safeway | `Allow: /`, some exclusions | Product pages permitted |
| Kroger | — | Use the official API instead |

The big chains mostly say no, and back it with bot protection a plain HTTP
request won't pass. Don't burn weeks fighting that. The wins are: official APIs
where they exist, and small local grocers, co-ops and ethnic markets — which
are frequently the cheaper source anyway.

## Files

| File | What it does |
| --- | --- |
| `app.py` | Flask routes |
| `db.py` | SQLite schema, indexes, sessions, settings |
| `security.py` | CSRF tokens, response headers, redirect checking |
| `auth.py` | Accounts, scrypt passwords, roles |
| `inventory.py` | Lots, FEFO rotation, the movement ledger |
| `validation.py` | Form input to values the app can trust |
| `markets.py` | The demo price simulator |
| `refresh.py` | One refresh path for the web button and the CLI |
| `shopping.py` | Low-stock detection, scoring, the trip plan |
| `seed.py` | Demo items and markets |
| `tests/` | 73 tests: security, auth, rotation, validation, scoring, schema |

## Stock, lots and rotation

`items.qty` is not a number anyone types. It is derived from **lots** — a lot
being one physical batch with its own expiry date — and every change to it goes
through the ledger in `inventory.py`, which records who moved what and why.
That is what turns the figure on screen into something you can audit.

Rotation is **FEFO**, first *expired* first out, not FIFO. The tin that arrived
first is not necessarily the tin that goes off first, and it is the expiry date
that decides whether food can be handed to someone. A distribution drains the
soonest-expiring lot, then the next, and leaves non-perishables until last.

```
Intake   20 cans, expires 05 Oct     ┐
Intake   30 cans, expires 11 Apr     │  distribute 35
Intake   42 cans, no date            ┘
                                     ↓
Takes 20 from the October lot, then 15 from April.
The undated stock is untouched.
```

Recording 35 going out therefore needs no decision from whoever is at the
keyboard — the order is a property of the stock, not a judgement call made
under pressure on a Tuesday afternoon.

Quantities cannot go negative: a distribution larger than what is on hand is
refused rather than silently booked, because a ledger that can lie is not
worth keeping.

## Who can do what

| | Volunteer | Admin |
| --- | :---: | :---: |
| Count stock, record intake and distribution | ✅ | ✅ |
| Write off expired lots | ✅ | ✅ |
| See the shopping list and rescan prices | ✅ | ✅ |
| Add and remove items | ✅ | ✅ |
| Delete items | — | ✅ |
| Markets and preferences | — | ✅ |
| Accounts | — | ✅ |

Passwords are hashed with scrypt from the standard library — deliberately slow
and memory-hard, so a stolen database cannot be run through a wordlist at
speed. The login gives the same answer and takes the same time whether the
username exists or not.

## Security

The app runs on a network where volunteers use shared machines, so it is
hardened rather than trusting its surroundings:

| Protection | What it stops |
| --- | --- |
| CSRF tokens on every form | Another site quietly POSTing `/items/3/delete` while a volunteer has the page open |
| Same-origin redirect check | `redirect(request.referrer)` sending staff to an attacker's page — the Referer header is set by the sender |
| Validation on every number | `float("abc")` 500s, and `nan`/`inf`/`1e308` silently poisoning every total |
| CSP, `nosniff`, `frame-ancestors 'none'` | Injected script running, and the app being framed for clickjacking |
| Debugger off by default | The Werkzeug console, which is a remote shell for anyone who can reach it |
| Generic 500 page | Stack traces and file paths leaking to whoever triggered the error |
| `HttpOnly` / `SameSite=Lax` session cookie | The session being read by script or sent cross-site |
| Price sanity bounds | A scraper that grabbed a zip code writing "$97205" into the shopping list |
| Rescan cooldown | A held-down button turning the food bank into a source of unwanted traffic |
| scrypt password hashing | A stolen database being cracked offline at speed |
| New session id on sign-in | Session fixation — a planted session surviving a login |
| Role checks on every admin route | A volunteer account reaching markets, settings or accounts |
| Last-admin guard | Locking everyone out of the install |

Secrets are never committed. `FOODBANK_SECRET_KEY` is read from the
environment; failing that a random key is generated into a `0600`
`.secret_key` file, which `.gitignore` excludes along with the database.

Set `FOODBANK_HTTPS=1` when serving over TLS so the session cookie is marked
`Secure`. `FOODBANK_DEBUG=1` enables the debugger — never on a shared machine.

## Performance

The shopping list opened a database connection per low item, never closed any
of them, and full-scanned `prices` each time. It now runs as one batched query
on one connection that closes.

| | Before | After |
| --- | --- | --- |
| Connections per shopping list | 18, none closed | 3, all closed |
| `SELECT`s per shopping list | 18, each a full scan | 3, all index-served |

Prices are looked up through a covering index on `(item_name, market_id,
price)`, so the query is answered from the index without touching the table.
`PRAGMA optimize` keeps the planner's statistics current as the data grows,
and the database runs in WAL mode so a read never blocks on a write.

## Not in the MVP

Barcode scanning, purchase orders, and multi-site inventory. The schema leaves
room for each: a `barcode` column on `items` and a lookup in the intake form;
purchase orders as a saved shopping list with a status; multi-site as a
`site_id` on `lots` and `items`.

Nothing here is multi-tenant. One food bank, one database, one site.
