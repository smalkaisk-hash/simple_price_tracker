# simple_price_tracker

This repository is private. This is python tracker for tech prices to track and find dipping prices.

Runs daily via GitHub Actions and alerts over Telegram. Currently tracks SATA
enterprise HDDs across four refurb/used shops, ranked by price per terabyte.

## Two kinds of watch

**`PRODUCTS`** — a single product URL, scraped with BeautifulSoup. Alerts when the
price moves by `PRICE_CHANGE_THRESHOLD` (5%) in either direction. Currently empty.

**`WATCHES`** — a whole category listing, ranked by price per TB. Alerts when a
drive appears at or under `MAX_EUR_PER_TB` (€25.00/TB of goods, before 21% VAT and
before delivery), or when a drive already under the threshold drops another 5%.

## Normalisation

Every shop is converted to **EUR per TB excluding VAT** before ranking, so shops in
different currencies and VAT conventions compare directly. That figure is the bar. Each
alert then also shows the **delivered cost in Latvia** — the same drive plus delivery
plus 21% Latvian VAT — so the real outlay is visible without letting it distort the
ranking.

FX comes from the ECB's free daily reference rates, **re-fetched at the start of every
run** — Techbuyer quotes GBP, so a stale rate would skew every UK figure at once. The
rates used are printed in each run's log.

**A shop's own VAT rate never enters the maths.** Intra-EU purchases on a Latvian PVN
number are reverse-charged, so German 19% is not what gets paid; a UK import is
zero-rated for export and then charged LV import VAT at the border. Either way the
rate that applies is Latvia's 21% (`LV_VAT_RATE`). UK orders also attract a courier
clearance fee, which is **not** modelled — a UK drive needs to beat an EU one by a
clear margin.

Three fields describe each shop:

- `price_basis` — whether the *scraped* field is `net` or `gross`
- `vat_rate` — strips VAT back off a `gross` shop to get net
- `display_vat_rate` — VAT the shop adds on the price **its own page shows**. Used
  only for the "shop shows" line on each alert, never for ranking.

| Shop | Currency | Scraped field | Its page displays |
|---|---|---|---|
| Techbuyer (UK) | GBP | net (`basePrice`, not `finalPrice`) | gross @ 20% — £228.94 → £274.73 |
| Renewtech (DK) | EUR | net | net, "zzgl. MwSt." — €238 → €238 |
| ServerShop24 (DE) | EUR | net (`priceNet`) | gross @ 19% — €252.09 → €299.99 |
| Gekko (DE) | EUR | net (`data-price`) | gross @ 19% — €410.92 → €489.00 |

Renewtech is the only shop that displays net, which is why it was the only one whose
alerts ever matched its own page.

## What an alert looks like

Every deal, from every shop, shows both €/TB figures and itemises tax and delivery:

```
TOSHIBA MG07ACA 14TB 6G 7.2K SATA (512e) 3.5" LFF — MG07ACA14TE
    €18.01/TB before tax & delivery — €252.09
    €23.09/TB delivered — €323.32
      goods €252.09 + €15.12 delivery + 21% VAT €56.11
    14TB · 6Gbps · shop shows €299.99 + €17.99 · view
```

The three itemised parts add up to the delivered total exactly, so any alert can be
checked by hand. `shop shows` is the number on the shop's own page, in its own currency
and VAT, so it can also be checked against the site at a glance.

**Ranking and the `MAX_EUR_PER_TB` threshold use the first figure only** — goods,
ex-VAT, ex-delivery. Tax and delivery are shown, never used to decide who alerts.
Delivery is a flat per-order charge, so folding it into the ranking would sort the list
by capacity as much as by value, and a shop editing its delivery table would fire
spurious price-drop alerts. For the same reason the 5%-drop comparison is made on the
pre-tax figure too.

## Delivery to Latvia

The delivered figure is **goods + delivery**, with delivery *inside* the VAT base — VAT
is due on the whole supply, and UK import VAT is levied on the customs value including
freight. So it is `(goods_net + delivery_net) × 1.21`. It is shown, not ranked on.

All four shops publish a Latvia-applicable delivery price on a page a plain GET can
read, so it is **re-read every run** rather than frozen into the source. Each watch
also carries a `fallback`: if a page is redesigned, a stale-but-sane charge beats
silently dropping delivery and under-reporting again. Every degradation is logged.

| Shop | Page | Latvia | Basis | Free over |
|---|---|---|---|---|
| Techbuyer | `/uk/delivery` | £30 ("Mainland Europe") | order value | £500 |
| Renewtech | `renewtech.de/delivery` | €20 ("innerhalb der EU") | flat | — |
| ServerShop24 | `/en/shipping/` | €17.99 (DHL Std International) | flat to 31.5 kg, then +€1/kg | DE only |
| Gekko | `/en/shipping_and_delivery.html?countryCode=LV` | €24.95 (DHL, ≤25 kg) | 8 weight bands | DE only |

Gekko is the only one with a genuinely per-country endpoint — `countryCode=LV`
re-renders the whole weight table server-side. The others are static tables, scraped
anyway so a change in them is picked up rather than quietly missed.

Two deliberate caveats, both stated in every message's footer:

- **Delivery is per order, not per drive.** Totals assume a one-drive order — exact
  for buying one, pessimistic for buying several.
- **The UK courier clearance fee is not modelled**, so Techbuyer's real landed cost is
  somewhat higher than shown.

Delivery is quoted on the same VAT basis as the prices each shop *displays*, so
`display_vat_rate` is what gets stripped from it — not `vat_rate`, which describes the
scraped product field and is a different basis on three of the four shops.

### Gekko has three prices in one tile

Only `data-price` is real. The tile also carries `content="…" itemprop="price"`, a
grid-only *"from"* price that is a flat gross × 0.88 across the whole catalogue and
appears nowhere on the product page. Scraping that one and then stripping 19% VAT off
it (it was never gross) reported €361.61 for a drive Gekko sells at €489.00 — 26%
under, on every Gekko alert. `data-price × 1.19` reproduces the shelf price to the
cent on all 130 catalogue items; `test_tracker.py` pins this.

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

Every history point is stamped with `basis` (`HISTORY_BASIS`). The weekly trend only
subtracts points on the current basis, so a change in what €/TB *means* — such as the
ex-VAT → incl-21%-VAT switch — cannot show up as an overnight 21% rise. Points from a
superseded basis are skipped and age out within 30 days.

## Tests

`python -m unittest` — no network, no extra dependencies. Fixtures are trimmed copies
of real shop markup, so a shop changing its HTML fails a test instead of quietly
sending a wrong price.

## Environment

`TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`, from `.env` locally or Actions secrets in
CI. There are **no fallback values in the source** — earlier revisions had them
hardcoded, so anything in git history before this point is compromised. Setting
`MANUAL_RUN=true` forces a summary even when nothing changed.

## Cost

A full run is ~6 minutes of scraping plus ~1 minute of CI overhead. Daily, that is
roughly 210 of the 2,000 monthly Actions minutes included for private repos.
