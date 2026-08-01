# simple_price_tracker

This repository is private. This is python tracker for tech prices to track and find dipping prices.

Runs daily via GitHub Actions and alerts over Telegram. Currently tracks SATA
enterprise HDDs across four refurb/used shops, ranked by price per terabyte.

## Two kinds of watch

**`PRODUCTS`** — a single product URL, scraped with BeautifulSoup. Alerts when the
price moves by `PRICE_CHANGE_THRESHOLD` (5%) in either direction. Currently empty.

**`WATCHES`** — a whole category listing, ranked by price per TB. Alerts when a
drive appears at or under `MAX_EUR_PER_TB`, or when a drive already under the
threshold drops another 5%.

## Normalisation

Every shop is converted to **EUR per TB excluding VAT** before ranking, so shops in
different currencies and VAT conventions compare directly. FX comes from the ECB's
free daily reference rates. `price_basis` says whether a shop's listed prices are
`net` or `gross`; `vat_rate` strips VAT back off the gross ones.

| Shop | Currency | Basis | Notes |
|---|---|---|---|
| Techbuyer (UK) | GBP | net | outside the EU — import VAT + clearance fee apply |
| Renewtech (DK) | EUR | net | product pages say "zzgl. MwSt." |
| ServerShop24 (DE) | EUR | net | prices land exactly on x.99 at ×1.19 |
| Gekko (DE) | EUR | gross | listing shows "incl. VAT", 19% stripped |

## Filters

- `name_filter` — regex against the product title (`\bSATA\b`)
- `min_tb` — small drives skew price/TB, so they are excluded
- `min_gbps` — SATA III only. Titles spell the rate as `6Gbps`, `6 Gbps`, `6Gb/s`
  or a bare `6G`, and some omit it; `allow_unstated_gbps` keeps the silent ones,
  which matters because the cheapest drive on Techbuyer's listing is one of them.
  A SATA listing claiming >6 Gbps is a mislabelled spec and gets clamped.
- Lots/multipacks and SSDs are dropped — both would otherwise read as cheap HDDs.

## Stock

**This is the difference between a real deal and a phantom.** Renewtech in
particular leaves sold-out drives listed at stale prices; before filtering it
looked 3× cheaper than everyone, and every one of those listings was unavailable.

Renewtech, ServerShop24 and Gekko publish stock in the listing itself. Techbuyer
does not, so watches with `verify_stock` do a per-product check at alert time —
covering both the alert candidates and the ranked list behind the weekly report,
so a sold-out drive cannot be recommended in either. Only an *explicit* out-of-stock
is dropped; unknown stays in, so a markup change degrades to over-alerting rather
than silently hiding everything.

## Links

Product URLs are resolved per shop, best source first: scraped from the listing
(Gekko, ServerShop24) > sitemap lookup (Techbuyer) > reconstructed from the title
(Renewtech, whose grid is JS-rendered with no anchors) > the shop's search page.
Reconstructed URLs are checked before they go in a message.

## Failure reporting

A scheduled run has no one reading its log, so a broken scraper would otherwise be
indistinguishable from "no deals this week". If a shop returns nothing, throws, or
matches nothing, it alerts once, stays quiet for the duration of the outage, repeats
weekly, and sends a recovery notice when it comes back. One shop failing never stops
the others.

## Scraping notes

Techbuyer and Renewtech render their grids client-side, so the URL's `lb.f[]` filter
params **do not survive a plain HTTP fetch**. Both embed the same products as a
Google Analytics `view_item_list` payload in the served HTML, which is the scrape
target — far more stable than CSS selectors. Pagination *is* server-side, so the
whole category is reachable by walking pages until no new SKUs appear.

Capacity is parsed from the product title, not a structured field; no shop exposes
it separately. A malformed title can mis-rank a drive.

## State

`prices.json` holds both watch types:

```json
{ "products": { }, "watches": { } }
```

Each category watch remembers only the drives currently under the threshold, so one
that lapses above it and later returns will alert again. 30 days of history is kept.

## Environment

`TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`, from `.env` locally or Actions secrets in
CI. There are **no fallback values in the source** — earlier revisions had them
hardcoded, so anything in git history before this point is compromised. Setting
`MANUAL_RUN=true` forces a summary even when nothing changed.

## Cost

A full run is ~6 minutes of scraping plus ~1 minute of CI overhead. Daily, that is
roughly 210 of the 2,000 monthly Actions minutes included for private repos.
