# simple_price_tracker

> Private repository.

A small, dependency-light Python tracker for second-hand enterprise storage prices.
It scrapes a handful of refurb/used server-parts shops once a day, normalises every
listing to **one comparable number — EUR per terabyte, before tax and delivery —**
ranks them, and pushes the good deals to **Telegram**. It runs itself on a free
**GitHub Actions** cron; there is no server to keep alive and no database.

Today it tracks **SATA enterprise HDDs** across five shops (Techbuyer, Renewtech,
ServerShop24, Gekko, Datablocks), but the watch model is generic: a watch is just a
listing URL, a parser, and a set of filters.

---

## Table of contents

- [Why it exists](#why-it-exists)
- [At a glance](#at-a-glance)
- [Repository layout](#repository-layout)
- [How a run works](#how-a-run-works)
- [The two kinds of watch](#the-two-kinds-of-watch)
- [The money math (normalisation)](#the-money-math-normalisation)
- [Scraping internals](#scraping-internals)
- [The filter pipeline](#the-filter-pipeline)
- [Stock verification](#stock-verification)
- [Product links](#product-links)
- [Alerts: every message and what triggers it](#alerts-every-message-and-what-triggers-it)
- [State file (`prices.json`)](#state-file-pricesjson)
- [Scheduling](#scheduling)
- [Configuration reference](#configuration-reference)
- [Running locally](#running-locally)
- [Tests](#tests)
- [Operations / runbook](#operations--runbook)
- [Extending the tracker](#extending-the-tracker)
- [Known limitations & caveats](#known-limitations--caveats)
- [Cost](#cost)

---

## Why it exists

Refurbished enterprise drives are the cheapest €/TB storage going, but the market is
noisy:

- Shops quote in **different currencies** (GBP, EUR) and on **different VAT bases**
  (some list net "plus VAT", some list gross).
- Delivery to Latvia is a **flat per-order charge** that varies by shop and swamps the
  per-TB figure on small drives.
- Listings go **stale**: sold-out drives linger at old prices, and mislabelled SAS
  drives show up under SATA searches.

The point of this tool is to turn all of that into a single honest ranking — *what
does a terabyte actually cost me, landed in Latvia?* — and to only shout when
something crosses a worthwhile bar.

## At a glance

| | |
|---|---|
| **Language** | Python 3.11 |
| **Dependencies** | `requests`, `beautifulsoup4`, `python-dotenv` (see `requirements.txt`) |
| **Entry point** | `python tracker.py` |
| **Scheduler** | GitHub Actions cron, daily at **09:00 UTC** (`.github/workflows/tracker.yml`) |
| **Output channel** | Telegram bot message (HTML parse mode) |
| **Persistence** | one JSON file, `prices.json`, committed back to the repo each run |
| **Secrets** | `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` (env / `.env` / Actions secrets) |
| **The bar** | `MAX_EUR_PER_TB = €25.00/TB`, measured on goods only (ex-VAT, ex-delivery) |

## Repository layout

```
tracker.py              # everything: config, scrapers, math, alerts, entry point
test_tracker.py         # offline unit tests (trimmed real-markup fixtures)
prices.json             # persisted state; rewritten and committed every run
requirements.txt        # requests, beautifulsoup4, python-dotenv
.github/workflows/
    tracker.yml         # the daily cron job
.env                    # local secrets (gitignored; never committed)
```

`tracker.py` is intentionally a single module. Reading it top to bottom follows the
pipeline: constants/config → FX & VAT helpers → shipping parsers → listing parsers →
pagination → ranking → URL/stock resolution → alert assembly → `main()`.

## How a run works

`main()` ([`tracker.py`](tracker.py)) is the whole lifecycle:

1. **`load_state()`** reads `prices.json` into `{ "products": {…}, "watches": {…} }`
   (migrating the old flat layout if it sees it).
2. **`is_weekly_run()`** → `True` on **Mondays** (`datetime.now().weekday() == 0`,
   evaluated in the runner's UTC clock).
3. **`manual`** is `True` when the environment variable `MANUAL_RUN == "true"` — set by
   the workflow whenever the job is triggered by hand (`workflow_dispatch`).
4. **`check_products(state, weekly, manual)`** — single-URL watches (currently none).
5. **`check_watches(state, weekly, manual)`** — the category watches; this is where all
   the real work happens.
6. **`save_state(state)`** writes `prices.json` back out.
7. **Credential check, last.** If `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` are missing the
   process raises `SystemExit` **after** state is saved — so a missing secret turns the
   CI run red instead of looking like a healthy run that silently sent nothing.

Within `check_watches`, FX rates are fetched **once per run** and every watch is
processed independently inside a `try/except`, so one shop changing its markup can
never take the other three down.

## The two kinds of watch

### `PRODUCTS` — single product URL (currently empty)

A list of `{name, url, currency}`. `get_price()` fetches the page with BeautifulSoup
and reads a WooCommerce price node (`.price .woocommerce-Price-amount`). It alerts when
the price moves by **`PRICE_CHANGE_THRESHOLD` (5%)** in either direction, posts a manual
confirmation on manual runs, and a weekly summary on Mondays. History is kept per
product (last 30 days).

> This path is wired up and tested-by-existence but `PRODUCTS = []` today; the action is
> all in `WATCHES`.

### `WATCHES` — a whole category listing

Each watch scrapes an entire category, ranks every matching drive by price per TB, and
alerts when a drive is **at or under `MAX_EUR_PER_TB`**, or when a drive already under
the bar **drops a further 5%**. Five watches are configured, one per shop.

## The money math (normalisation)

This is the heart of the tracker. Every listing, whatever its currency or VAT
convention, is reduced to **EUR per TB excluding VAT** so the shops compare in one
ranking — and then each alert *additionally* shows the **delivered cost in Latvia** so
the real outlay is visible without letting it distort the ranking.

### The one figure everything ranks on

```
eur_net_per_tb  =  (scraped price → net → EUR)  /  capacity_TB
```

- **`price_basis`** says whether the scraped field is `net` or `gross`. If `gross`,
  `to_eur_net()` strips VAT back off using the shop's **`vat_rate`** before converting.
- Conversion to EUR (`_to_eur()`) divides by the ECB rate (ECB quotes *units of the
  foreign currency per 1 EUR*). EUR shops pass through untouched.

**The threshold and the ranking use this pre-tax, pre-delivery figure only.** Delivery
is a flat per-order charge; folding it into the ranking would sort the list by capacity
as much as by value, and a shop editing its delivery table would fire spurious
price-drop alerts. The 5%-drop comparison is made on this figure too.

### FX rates

`fetch_eur_rates()` pulls the **ECB daily reference rates** (free, key-less XML) at the
start of every run. Techbuyer quotes GBP, so a stale rate would skew every UK figure at
once — hence re-fetched, never frozen. The rates in force are printed to the run log. If
the fetch fails, EUR shops still work and non-EUR shops are skipped (better than alerting
on a guessed rate).

### The delivered total (shown, never ranked on)

`landed_breakdown()` itemises what one drive costs delivered to Latvia:

```
delivered_total = round(goods_net_eur, 2)
                + round(delivery_net_eur, 2)
                + round((goods_net_eur + delivery_net_eur) × 0.21, 2)
```

- **A shop's own VAT rate never enters the maths.** Intra-EU purchases on a Latvian PVN
  number are **reverse-charged** (so German 19% is never paid); a UK import is
  **zero-rated for export** and then charged **LV import VAT at the border**. Either way
  the rate that applies is Latvia's **21%** (`LV_VAT_RATE`).
- **Delivery sits inside the VAT base.** VAT is due on the whole supply, and UK import
  VAT is levied on the customs value *including* freight — so it is
  `(goods + delivery) × 1.21`, not `goods × 1.21`.
- **Parts are rounded, then summed from the rounded parts**, because every alert prints
  all three. A total a cent adrift from its own breakdown reads as a bug.

### Reconciling against the shop's page

Because three of the five shops *display* gross while the *scraped* field is net, a raw
alert used to look wildly cheaper than the site. `shop_shelf_price()` =
`price × (1 + display_vat_rate)` reproduces the number on the shop's own page, carried
on every alert purely so a deal can be checked against the site at a glance.

| Shop | Currency | Scraped field | Its page displays |
|---|---|---|---|
| Techbuyer (UK) | GBP | net (`basePrice`, not `finalPrice`) | gross @ 20% — £228.94 → £274.73 |
| Renewtech (DK) | EUR | net | net, "zzgl. MwSt." — €238 → €238 |
| ServerShop24 (DE) | EUR | net (`priceNet`) | gross @ 19% — €252.09 → €299.99 |
| Gekko (DE) | EUR | net (`data-price`) | gross @ 19% — €410.92 → €489.00 |
| Datablocks (NL) | EUR | **gross** (`variant.price`) | gross @ 21% — the price *is* the shelf price |

Datablocks is the one shop whose scraped field is already **gross**: its Shopify feed
returns the BTW-inclusive shelf price, so `vat_rate` (21%) strips VAT *off* it to reach
net — the reverse of the three shops above. Renewtech is the only shop that displays
net, which is why it was the only one whose alerts ever matched its own page unaided.

### Three fields describe each shop's VAT

- **`price_basis`** — whether the *scraped* field is `net` or `gross`.
- **`vat_rate`** — only used to strip VAT back off a `gross` shop to get net.
- **`display_vat_rate`** — VAT the shop adds on the price *its own page shows*. Used for
  the "shop shows" line **and** to strip VAT off the shop's quoted *delivery* charge
  (delivery is quoted on the same basis as displayed prices), **never** for ranking.

### Delivery to Latvia

Four of the five shops publish a Latvia-applicable delivery price on a page a plain GET
can read, so it is **re-read every run** (`fetch_shipping()`), not frozen into the
source. Each watch carries a **`fallback`**: if a page is redesigned — or, for
Datablocks, where there is no scrapable per-country table at all — a stale-but-sane
charge beats silently dropping delivery and under-reporting again. Every degradation is
logged.

| Shop | Page | Latvia | Basis | Free over |
|---|---|---|---|---|
| Techbuyer | `/uk/delivery` | £30 ("Mainland Europe") | order value | £500 |
| Renewtech | `renewtech.de/delivery` | €20 ("innerhalb der EU") | flat | — |
| ServerShop24 | `/en/shipping/` | €17.99 (DHL Std International) | flat to 31.5 kg, then +€1/kg | DE only |
| Gekko | `/en/shipping_and_delivery.html?countryCode=LV` | €24.95 (DHL, ≤25 kg) | 8 weight bands | DE only |
| Datablocks | — (no scrapable table) | €15.50 fallback | EU "minimum", per policy | — |

The first four shops each have their own parser (`shipping_techbuyer`,
`shipping_renewtech`, `shipping_servershop24`, `shipping_gekko`) anchored so it can't
pick up the wrong table — e.g. Techbuyer's cheaper UK-only band sits *above* "Mainland
Europe" on the same page, and ServerShop24's German domestic rate (with a free-over-400
tier Latvia doesn't get) sits above the international one. Gekko is the only genuinely
per-country endpoint: `?countryCode=LV` re-renders the whole weight table server-side.
Datablocks has no parser — its policy only quotes EU delivery as a "minimum of €15.50",
so that constant is the fallback and `fetch_shipping()` returns it directly.

`shipping_charged()` honours any `free_over` tier (compared against the **net** price,
which errs towards charging delivery — safe, since that can only ever over-state a
total).

### Gekko has three prices in one tile

Only **`data-price`** is real. The tile also carries `content="…" itemprop="price"`, a
grid-only *"from"* price that is a flat `gross × 0.88` across the whole catalogue and
appears nowhere on the product page. Scraping *that* one and then stripping 19% VAT off
it (it was never gross) reported €361.61 for a drive Gekko sells at €489.00 — 26% under,
on every Gekko alert, until it was fixed. `data-price × 1.19` reproduces the shelf price
to the cent on all ~130 catalogue items; `test_tracker.py` pins this.

## Scraping internals

### Where the data lives per shop

| `source` | Shops | Scrape target |
|---|---|---|
| `ga4` | Techbuyer, Renewtech | Google Analytics `view_item_list` JSON embedded in the served HTML |
| `servershop24` | ServerShop24 | PlentyMarkets `products.push({…})` server-side script block |
| `gekko` | Gekko | `<article data-id="AL.Item" …>` tiles with `data-*` attributes |
| `datablocks` | Datablocks | Shopify `.../products.json` — a real JSON API, no HTML parsing |

Techbuyer and Renewtech render their product grids **client-side**, so the URL's
`lb.f[]` filter params don't survive a plain HTTP fetch — but both embed the same
products as a GA4 payload, which is far more stable than CSS selectors. Both Magento
shops emit byte-identical payloads, so they share one regex and `parse_ga4()`.

Datablocks is a **Shopify** store, which exposes the whole collection as structured
JSON at `.../products.json` — one item per variant, with a numeric price, SKU, and an
`available` boolean. That makes it the least fragile source of the five: no regex, no
markup coupling, and stock comes straight from the feed (so no `verify_stock`), while
the product link is built directly from each product's `handle`.

### Pagination

`fetch_page_items(url, page, source)` fetches one page (each shop paginates with its own
query param: `p` for GA4, `page` for the others) with up to **`PAGE_RETRIES` (3)**
attempts and a short **`PAGE_RETRY_DELAY` (5s)** backoff. `fetch_category()` walks pages
until:

- a page returns **no items** (end of listing), or
- a page yields **no new SKUs** (pagination wrapped around), or
- a fetch **fails outright** (keep what we have rather than lose the whole run), or
- **`max_pages`** is reached.

It sleeps **`PAGE_DELAY` (0.4s)** between pages to be polite, dedupes by `item_id`, and
logs `Scraped N products from <url> (P pages)`. `fetch_watch()` then merges every
category URL for a watch (Renewtech is brand-scoped, so it has seven) deduped by SKU.

### Stock parsing

`parse_ga4` recovers stock from the per-item schema.org `availability` block where one
exists (Renewtech), searching only up to where the *next* product starts so a short
block can't borrow the next drive's state. Techbuyer's category page has no such block,
leaving `in_stock = None` — resolved lazily later (see below). ServerShop24 and Gekko
read a live stock count / colour-coded badge from each tile.

## The filter pipeline

`rank_by_price_per_tb()` applies these gates **in order**; a drive must pass all of them
to be ranked:

1. **Valid price** — numeric and `> 0` (drops bundles and "call for price").
2. **`name_filter`** — the title must match the watch's regex (`\bSATA\b`).
3. **Not an SSD / not a multipack** — `SSD_RE` (`SSD`, `NVMe`, `M.2`, "solid state")
   and `LOT_RE` (`lot of`, `bundle`, `N x M`, `pack of`, `qty N`).
4. **Not SAS** — `SAS_RE` drops any title naming `SAS` (incl. `NL-SAS`, `SAS-3`), even
   when it *also* says SATA. A SAS drive won't run in a SATA-only port, so it must never
   be recommended.
5. **Capacity** — `capacity_tb()` parses TB/GB from the title; drops anything below
   **`min_tb` (4 TB)** (small drives skew €/TB).
6. **Interface ceiling** — the rate is read **unclamped**; a title stating **more than
   6 Gbps** (SATA's physical max; 12 Gbps is SAS-3) is dropped as a mislabelled SAS
   drive. *This is the second SAS tell* — it catches drives like Techbuyer's
   `Dell FN7VR 12TB … SATA … 12Gbps` that say "SATA" but quote a SAS speed.
7. **`min_gbps` (6)** — SATA III only. Titles spell the rate `6Gbps`, `6 Gbps`, `6Gb/s`
   or a bare `6G`; some omit it. **`allow_unstated_gbps` (True)** keeps the silent ones,
   which matters because the cheapest drive on Techbuyer's listing is often one of them.
8. **Stock** — an *explicit* out-of-stock is dropped; `None` ("shop didn't say") stays
   in, so a markup change degrades to over-alerting rather than silently hiding
   everything.

Surviving drives become deal dicts carrying the native price, the pre-tax €/TB, the
itemised delivered total, the shop's shelf price, interface, stock, and URL. The list is
sorted ascending by `eur_net_per_tb`. `qualifies(deal, threshold)` is simply
`eur_net_per_tb ≤ threshold`.

> **Both SAS tells matter.** The word check catches "SATA + SAS" titles; the >6 Gbps
> check catches a "SATA" title quoting a 12 Gbps SAS speed with no "SAS" word. Capacity
> is parsed from the *title*, not a structured field — a malformed title can still
> mis-rank a drive.

## Stock verification

> **This is the difference between a real deal and a phantom.** Renewtech in particular
> leaves sold-out drives at stale prices; before filtering it looked 3× cheaper than
> everyone, and every one of those listings was unavailable.

Renewtech, ServerShop24, Gekko and Datablocks publish stock in the listing itself
(Datablocks most cleanly of all, as a plain `available` boolean in its JSON feed).
Techbuyer does not, so watches with **`verify_stock: True`** do a per-product check
(`product_in_stock()`) at alert time — reading the product page's schema.org
availability. This runs only for the handful of drives about to be messaged: the alert
candidates **and** the qualifying drives behind the weekly/manual reports and the
leaderboard — so a sold-out drive can't be recommended in any of them. An unreadable
page returns `None` and is treated as in-stock (degrade to over-alerting, never silently
suppress).

## Product links

URLs are resolved per shop, best source first (`resolve_urls()`), and only ever for the
drives about to go into a message:

```
scraped from the listing        >  sitemap lookup  >  reconstructed from the title  >  shop search page
   (Gekko, ServerShop24,            (Techbuyer)         (Renewtech, JS grid, no anchors)
    Datablocks /products/{handle})
```

Reconstructed URLs (Renewtech) are HEAD-checked before they go in a message — a dead
link is worse than a search page. **Techbuyer's GA4 feed carries no product URL at all**,
so its links are rebuilt entirely from the sitemap; a SKU the sitemap doesn't list
(Synology drives, for one) falls back to Techbuyer's search page rather than appearing
with no link.

## Alerts: every message and what triggers it

All messages are Telegram HTML. `format_deal()` renders one drive like this:

```
TOSHIBA MG07ACA 14TB 6G 7.2K SATA (512e) 3.5" LFF — MG07ACA14TE
    €18.01/TB before tax & delivery — €252.09
    €23.09/TB delivered — €323.32
      goods €252.09 + €15.12 delivery + 21% VAT €56.11
    14TB · 6Gbps · shop shows €299.99 + €17.99 · view
```

The three itemised parts add up to the delivered total exactly, so any alert can be
checked by hand. A `EUR_NOTE` footer (one line, once per message) explains the basis.

| Message | When it fires | Contents |
|---|---|---|
| 🆕 **Now watching** | first time a watch is ever seen (`first_run`) and it has qualifying drives | up to 10 qualifying drives |
| 💰 **New deal** | a qualifying drive not seen under the bar before | up to 10 new qualifiers |
| 📉 **Price drop** | a drive already under the bar drops a further **≥5%** (compared pre-tax) | the drive + its old €/TB and % change |
| ℹ️ **Manual check** | manual run (`MANUAL_RUN=true`) that isn't a first run | up to 10 qualifying drives, or "Nothing under €25.00/TB right now." |
| 📊 **Weekly report** | Mondays | qualifying drives + a 7-day best-€/TB trend, or the "Nothing under…" line |
| 🏆 **Best €/TB across all shops** | Mondays or manual runs, if anything qualifies anywhere | the pooled qualifying drives from all shops, cheapest 10 |
| ⚠️ **Shop failing / ✅ recovered** | a shop returns nothing/throws/matches nothing (see below) | one-off failure notice, then a recovery notice |

### Summaries list qualifying drives only

The **Manual**, **Weekly**, and **🏆 leaderboard** summaries list **only drives at or
under €25/TB**. If a shop has none, its report says *"Nothing under €25.00/TB right
now."*; if nothing qualifies in *any* shop, the leaderboard is skipped entirely.

> This was a deliberate change. The summaries used to show the best five drives per shop
> *regardless* of the bar, which surfaced over-€25/TB drives on quiet days. They are now
> threshold-gated, so a quiet day may legitimately show just one or two drives — or none.

### Failure reporting

A scheduled run has no one reading its log, so a broken scraper would otherwise be
indistinguishable from "no deals this week". `report_watch_failure()` makes a shop that
returns nothing, throws, or matches nothing **alert once**, then stay quiet for the
duration of the outage, **repeat weekly**, and send a **recovery notice** when it comes
back. One shop failing never stops the others.

## State file (`prices.json`)

One JSON file holds both watch types and is committed back to the repo each run:

```json
{
  "products": { "<name>": { "price": 0, "last_checked": "…", "history": [] } },
  "watches": {
    "<watch name>": {
      "last_checked": "YYYY-MM-DD HH:MM",
      "best_eur_per_tb": 20.43,
      "matched": 51,
      "threshold": 25.0,
      "deals": {
        "<sku>": { "name": "…", "price": 0, "tb": 0, "per_tb": 0,
                   "eur_price": 0, "eur_per_tb": 0,
                   "eur_net_price": 0, "eur_net_per_tb": 0,
                   "eur_shipping": 0, "shop_price": 0, "shop_shipping": 0 }
      },
      "history": [ { "date": "YYYY-MM-DD", "best_eur_per_tb": 0,
                     "matched": 0, "basis": "eur_incl_lv_vat" } ]
    }
  }
}
```

Key behaviours:

- **Only qualifying drives are remembered** (`deals`). A drive that lapses above the bar
  and later returns will therefore alert again as new.
- **30 days of history** is kept per watch, recording the best €/TB and the matched
  count each day.
- **Each history point is stamped with `basis` (`HISTORY_BASIS`)**, a version guard.
  `trend_baseline()` only compares points on the current basis, so redefining what €/TB
  *means* (e.g. the historical ex-VAT → incl-VAT switch) can never read as an overnight
  jump; superseded points are skipped and age out within 30 days.
- The daily GitHub Actions job commits the rewritten file as `Update prices <date>`.

## Scheduling

`.github/workflows/tracker.yml`:

```yaml
on:
  schedule:
    - cron: '0 9 * * *'     # daily, 09:00 UTC
  workflow_dispatch:         # manual "Run workflow" button
```

- **Daily, 09:00 UTC** → ~11:00 Latvia in winter (EET), ~12:00 in summer (EEST).
- **Weekly report** is not a separate schedule — the Monday run sets `is_weekly_run()`
  and adds the 📊 and 🏆 messages on top of the usual alerts.
- **Manual dispatch** sets `MANUAL_RUN=true`, which forces the ℹ️ summary even when
  nothing changed.
- The job installs deps, runs `python tracker.py` with the Telegram secrets, then
  commits `prices.json` back (`contents: write` permission) as a `price-bot` user.

> GitHub's scheduled Actions can start **late** under load and are **skipped if the repo
> has had no activity for ~60 days**, so "09:00 UTC" is a target, not a guarantee. The
> daily price commits normally keep the repo active enough to avoid the 60-day pause.

## Configuration reference

All configuration lives in constants at the top of `tracker.py`.

### Global constants

| Constant | Value | Meaning |
|---|---|---|
| `LV_VAT_RATE` | `0.21` | Latvian VAT, the only rate applied to landed cost |
| `MAX_EUR_PER_TB` | `25.0` | the alert bar, on goods only (ex-VAT, ex-delivery) |
| `PRICE_CHANGE_THRESHOLD` | `0.05` | 5% move needed to re-alert a known drive / a product |
| `MAX_RETRIES` / `RETRY_DELAY` | `3` / `900s` | single-product fetch retries (15-min backoff) |
| `PAGE_RETRIES` / `PAGE_RETRY_DELAY` | `3` / `5s` | category page fetch retries |
| `PAGE_DELAY` | `0.4s` | politeness delay between category pages |
| `SATA_MAX_GBPS` | `6.0` | interface ceiling; a stated higher rate ⇒ SAS ⇒ dropped |
| `HISTORY_BASIS` | `"eur_incl_lv_vat"` | version stamp guarding the weekly trend series |

### Per-watch fields (`WATCHES`)

| Field | Purpose |
|---|---|
| `name` | human label, and the key under `state["watches"]` |
| `source` | parser selector: `ga4` / `servershop24` / `gekko` / `datablocks` |
| `urls` | one or more category listing URLs (merged, deduped by SKU) |
| `currency` / `currency_code` | display symbol / ISO code for FX lookup |
| `price_basis` | `net` or `gross` — basis of the scraped price field |
| `vat_rate` | strips VAT off a `gross` scraped price |
| `display_vat_rate` | VAT the shop's page shows; strips VAT off quoted delivery; drives the "shop shows" line |
| `shipping` | `{source, url, fallback, free_over?}` for `fetch_shipping()` |
| `name_filter` | regex the title must match (`\bSATA\b`) |
| `min_tb` | minimum capacity (4.0) |
| `min_gbps` | minimum interface rate (6.0 = SATA III) |
| `allow_unstated_gbps` | keep drives whose title omits the rate |
| `max_pages` | pagination cap |
| `sitemap` | sitemap URL for link resolution (Techbuyer) |
| `url_builder` | reconstruct a URL from the title (Renewtech) |
| `search_url` | last-resort search link template (`{q}`) |
| `verify_stock` | per-product stock check at alert time (Techbuyer) |

## Running locally

```bash
python -m venv .venv
. .venv/Scripts/activate        # Windows; use .venv/bin/activate on *nix
pip install -r requirements.txt

# Secrets (optional locally): create .env
#   TELEGRAM_TOKEN=...
#   TELEGRAM_CHAT_ID=...

python tracker.py
```

Without Telegram secrets the run still scrapes and rewrites `prices.json`, prints every
alert it *would* have sent, and then exits non-zero on the final credential check.
Set `MANUAL_RUN=true` to force the manual summary. A full run is ~6 minutes of scraping
(Techbuyer alone is ~55 pages); the EU shops are much faster.

## Tests

```bash
python -m unittest           # or: python -m unittest -v
```

**No network, no extra dependencies.** Fixtures are trimmed copies of real shop markup,
so a shop changing its HTML fails a test instead of quietly sending a wrong price.
Coverage includes: the Gekko three-prices tile, Latvian-VAT landed math, shelf-price
reconciliation, every shipping parser (and its fallback), the pre-tax ranking basis, the
cost breakdown adding up to the cent, the weekly-trend basis guard, the SAS filters
(word + >6 Gbps, plus the "part number isn't a speed" guard), the Techbuyer search-link
fallback, and the threshold-gated summaries (via a mocked `check_watches`).

## Operations / runbook

- **"No deals this week" — is it broken or just quiet?** Check the Actions log. A broken
  shop sends a ⚠️ failure alert and logs it; genuine quiet shows `N matched, 0 under
  €25.00/TB`. The summaries now say "Nothing under €25.00/TB" explicitly.
- **A shop's figures look wrong.** Each deal stores the native price, shop shelf price
  and shop shipping in `prices.json` for auditing, so a suspicious alert can be traced
  straight back to the page. The run log prints the ECB rates and each shop's resolved
  delivery charge.
- **A shop was redesigned.** Expect a failed test (if its markup fixture changed) or a
  ⚠️ failure alert (if it returns nothing). Shipping falls back to the configured
  constant and logs the degradation; prices do not — a shop that matches nothing is
  reported, not guessed.
- **Secrets missing in CI.** The run goes red on the final `SystemExit` *after* saving
  prices — deliberately, so it can't look green while dropping every alert.
- **Rotating the bot token.** Earlier revisions hardcoded credentials; **anything in git
  history before they were removed is compromised and must be treated as rotated.** There
  are no fallback credentials in the source.

## Extending the tracker

### Add another shop (category watch)

1. Write a parser `parse_<source>(html) -> [ {item_id, item_name, item_brand, price,
   url?, in_stock?, stock_qty?}, … ]` and register it in `PARSERS` (and add its
   pagination param to `PAGE_PARAM`).
2. If the shop publishes a Latvia delivery page, add a `shipping_<source>()` parser to
   `SHIPPING_PARSERS`; always set a `fallback`.
3. Add a `WATCHES` entry with the VAT fields (`price_basis`, `vat_rate`,
   `display_vat_rate`), filters, and link-resolution hints (`sitemap` / `url_builder` /
   `search_url`).
4. Add a trimmed-markup fixture and tests to `test_tracker.py` — especially a shelf-price
   reconciliation so the net/gross basis is pinned.

### Track a single product

Append `{ "name": "...", "url": "...", "currency": "€" }` to `PRODUCTS`. `get_price()`
currently targets a WooCommerce price node; a different storefront needs its selector
adjusted.

### Change the bar or capacity floor

Edit `MAX_EUR_PER_TB` (the €/TB alert bar) or a watch's `min_tb` / `min_gbps`.

## Known limitations & caveats

- **Delivery is per order, not per drive.** Totals assume a one-drive order — exact for
  buying one, pessimistic for buying several. Stated in every message footer.
- **The UK courier clearance fee is not modelled**, so Techbuyer's real landed cost is
  somewhat higher than shown — a UK drive needs to beat an EU one by a clear margin.
- **Capacity and interface come from the title**, not structured fields. A malformed
  title can mis-rank a drive, and a SAS drive mislabelled "SATA" with *no* stated speed
  and *no* "SAS" word is undetectable from the title alone.
- **GitHub cron is best-effort** — late starts and the 60-day inactivity pause apply.
- **Scrapers are markup-coupled by nature.** The GA4/JSON targets are more stable than
  CSS selectors, but a storefront re-platforming will still need parser work (the tests
  are the early-warning system).

## Cost

A full run is ~6 minutes of scraping plus ~1 minute of CI overhead. Daily, that is
roughly **210 of the 2,000 monthly Actions minutes** included free for private repos.
ECB FX and Telegram are both free. There is no server and no database to pay for.
