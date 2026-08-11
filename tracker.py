import html as html_lib
import requests
from urllib.parse import quote_plus
import json
import os
import re
import time
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# No fallbacks on purpose: credentials come from .env locally and from Actions
# secrets in CI. The literals that used to sit here are still in git history, so
# the bot token they contained must be treated as compromised and rotated.
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Single-product watches: exact URL, scraped price.
PRODUCTS = []

# Every watch is normalised to EUR per TB *including 21% Latvian VAT* before ranking,
# so shops in different currencies and VAT conventions compare in one list and every
# figure is the landed cost in Latvia. A shop's own VAT rate never enters the maths:
# intra-EU purchases are reverse-charged, so German 19% is not what gets paid, and a
# UK import is zero-rated for export then charged LV import VAT at the border. A UK
# drive additionally attracts a courier clearance fee that is *not* modelled here, so
# it still needs to beat an EU one by a clear margin.
LV_VAT_RATE = 0.21

# Alert threshold, measured on goods alone -- before 21% VAT and before delivery (the
# older £22/TB was ~EUR 25.7/TB). Tax and delivery are broken out on every alert so the
# real cost is visible, but they never decide who alerts or who ranks first: delivery is
# a flat per-order charge, so folding it in would reorder the list by capacity rather
# than by value, and a shop changing its delivery table would fire price alerts.
MAX_EUR_PER_TB = 25.0

# Category watches: scrape a whole listing, rank by price per TB, alert on cheap drives.
#   source            -- which parser to use (see PARSERS)
#   price_basis       -- "net" if scraped prices exclude VAT, "gross" if they include it
#   vat_rate          -- only used to strip VAT back off a "gross" shop
#   display_vat_rate  -- VAT the shop adds on the price *its own page* shows, purely so
#                        an alert can be reconciled with the page. Never used in ranking:
#                        what gets paid is LV_VAT_RATE, not the shop's local rate.
#   shipping          -- where to read delivery to Latvia, plus a fallback charge and
#                        any free-over-N tier. See fetch_shipping().
WATCHES = [
    {
        "name": "Techbuyer SATA HDD",
        "source": "ga4",
        "urls": ["https://www.techbuyer.com/uk/server-parts/server-storage/hdd-hard-disk-drives"],
        "currency": "£",
        "currency_code": "GBP",
        "price_basis": "net",         # the GA4 feed is ex-VAT (basePrice, not finalPrice)
        "display_vat_rate": 0.20,     # the page headline is inc-VAT: 228.94 -> £274.73
        # Banded by order value, not weight, and Latvia sits in "Mainland Europe".
        "shipping": {"source": "techbuyer", "fallback": 30.0, "free_over": 500.0,
                     "url": "https://www.techbuyer.com/uk/delivery"},
        "name_filter": r"\bSATA\b",   # only SATA drives
        "min_tb": 4.0,                # ignore small drives, they skew price/TB
        "min_gbps": 6.0,              # SATA III only; excludes 3Gbps SATA II drives
        "allow_unstated_gbps": True,  # see transfer_gbps(); set False to require it
        "max_pages": 80,
        "sitemap": "https://www.techbuyer.com/sitemap.xml",
        # Techbuyer's category HTML carries no stock field, so stock is confirmed
        # per-product just before alerting. The EU shops publish it in the listing.
        "verify_stock": True,
    },
    {
        # Danish (Kimbrer Computer, Aars) despite the .de domain -- inside the EU,
        # so no customs. Uses the exact same GA4 payload as Techbuyer. Categories
        # are brand-scoped only; there is no combined "all hard drives" listing.
        "name": "Renewtech SATA HDD",
        "source": "ga4",
        "urls": [f"https://www.renewtech.de/{brand}/harddrives.html" for brand in
                 ("seagate", "western-digital", "toshiba", "hp", "dell", "lenovo", "ibm")],
        "currency": "€",
        "currency_code": "EUR",
        "price_basis": "net",         # product pages say "zzgl. MwSt." (plus VAT)
        "display_vat_rate": 0.0,      # ...and display that net figure, so nothing to add
        # One flat charge for every country inside the EU; no free-delivery tier.
        "shipping": {"source": "renewtech", "fallback": 20.0,
                     "url": "https://www.renewtech.de/delivery"},
        "name_filter": r"\bSATA\b",
        "min_tb": 4.0,
        "min_gbps": 6.0,
        "allow_unstated_gbps": True,
        "max_pages": 20,
        "url_builder": "renewtech",
        "search_url": "https://www.renewtech.de/catalogsearch/result/?q={q}",
        # Most items publish stock in the listing, but a minority have no schema.org
        # block there; this confirms those few at alert time rather than guessing.
        "verify_stock": True,
    },
    {
        # Prices are embedded net: every one lands exactly on EUR x.99 at x1.19.
        "name": "ServerShop24 SATA HDD",
        "source": "servershop24",
        "urls": ["https://www.servershop24.de/en/components/hard-disks/sata/hdds/"],
        "currency": "€",
        "currency_code": "EUR",
        "price_basis": "net",
        "display_vat_rate": 0.19,     # page shows gross: priceNet 252.09 -> €299.99
        # DHL Standard International: one flat rate to 31.5kg, i.e. ~45 drives. The
        # free-over-400 tier on the same page is Germany only and does not apply.
        "shipping": {"source": "servershop24", "fallback": 17.99,
                     "url": "https://www.servershop24.de/en/shipping/"},
        "name_filter": r"\bSATA\b",
        "min_tb": 4.0,
        "min_gbps": 6.0,
        "allow_unstated_gbps": True,
        "max_pages": 5,               # small catalogue, ~4 SATA HDDs
    },
    {
        "name": "Gekko SATA HDD",
        "source": "gekko",
        "urls": ["https://www.gekko-computer.de/en/c/Parts/Hard-Drives/SATA"],
        "currency": "€",
        "currency_code": "EUR",
        # The tile *displays* gross, but the data-price attribute scraped from it is
        # already net, so there is nothing left to strip.
        "price_basis": "net",
        "display_vat_rate": 0.19,     # data-price 410.92 x1.19 -> the €489.00 on the page
        # The only shop with a genuinely per-country endpoint: countryCode=LV re-renders
        # the whole weight table server-side. Free delivery is Germany only.
        "shipping": {"source": "gekko", "fallback": 24.95,
                     "url": "https://www.gekko-computer.de/en/shipping_and_delivery"
                            ".html?countryCode=LV"},
        "name_filter": r"\bSATA\b",
        "min_tb": 4.0,
        "min_gbps": 6.0,
        "allow_unstated_gbps": True,
        "max_pages": 10,
    },
]

PRICE_LOG = "prices.json"

PRICE_CHANGE_THRESHOLD = 0.05  # 5%
MAX_RETRIES = 3
RETRY_DELAY = 900  # 15 minutes between retries

# Category pages are fetched dozens at a time, so they use a short backoff
# instead of the 15-minute one used for single products.
PAGE_RETRIES = 3
PAGE_RETRY_DELAY = 5
PAGE_DELAY = 0.4  # be polite between pages

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)

SITEMAP_URL = "https://www.techbuyer.com/sitemap.xml"

# ECB publishes daily EUR reference rates as free, key-less XML.
ECB_FX_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

# Techbuyer renders its product grid in JavaScript, but embeds the same products
# as a Google Analytics view_item_list payload in the served HTML. That payload is
# the scrape target -- far more stable than CSS selectors. Renewtech's Magento
# shop emits byte-identical payloads, so both use this one regex.
ITEM_RE = re.compile(r'\{"item_name":".*?"index":\d+\}')
CAPACITY_RE = re.compile(r'(?<![\d.])(\d+(?:\.\d+)?)\s*(TB|GB)\b', re.I)
LOC_RE = re.compile(r"<loc>(https://www\.techbuyer\.com/uk/[^<]+)</loc>")
FX_RE = re.compile(r"currency='([A-Z]{3})'\s+rate='([\d.]+)'")

# ServerShop24 (PlentyMarkets) writes the listing into a server-side script block.
# The '{"id"' anchor matters: the page also contains a JS *template* using the same
# products.push() call, and matching that yields garbage.
SS24_RE = re.compile(r'products\.push\((\{"id".*?\})\);')
# The feed's own "id" is not the one in the product URL, but its "category" field is
# exactly the URL slug, so the listing's anchors give a non-positional way to match.
SS24_HREF_RE = re.compile(r'href="(/en/([a-z0-9-]+)/a-\d+/)"')

# ServerShop24 shows a live stock count per tile, tied to the product by the image's
# data-alt (the feed JSON itself carries no stock field).
SS24_STOCK_RE = re.compile(r'Aktueller Lagerbestand: (\d+)')
SS24_ALT_RE = re.compile(r'data-alt="([^"]*)"')

# Gekko puts everything on the <article> wrapper as data-* attributes.
# Three different numbers sit in one tile, and only data-price is the real one:
#   data-price="410.92"           -- net; x1.19 == 489.00, the price the product page shows
#   content="430.32" itemprop=..  -- the grid's "from" price, a flat gross x 0.88 across
#                                    the whole catalogue, and absent from the product page
# Scraping the itemprop and then stripping 19% VAT off it reported EUR 361.61 for a
# drive the shop sells at EUR 489.00 -- 26% under, on every Gekko alert ever sent.
GEKKO_PRICE_RE = re.compile(r'data-price="([\d.]+)"')
GEKKO_HREF_RE = re.compile(r'href="(/en/p/[^"]+)"')
GEKKO_AVAIL_RE = re.compile(r'<div class="availability (\w+)"')

# schema.org stock markers. Both shops escape the slashes, and Techbuyer still emits
# the http:// form, so the host part is skipped rather than matched literally.
LD_PRODUCT_RE = re.compile(r'"@type":"Product","name":"((?:[^"\\]|\\.)*)"')
LD_AVAIL_RE = re.compile(r'"availability"\s*:\s*"[^"]*?(InStock|OutOfStock|BackOrder|PreOrder|SoldOut|Discontinued)"', re.I)

# Transfer rate. SATA III is 6 Gbps and SATA II is 3 Gbps, but shops spell the rate
# four different ways -- "6Gbps", "6 Gbps", "6Gb/s", or a bare "6G" -- and some omit
# it entirely. The G(?![Bb]) branch catches the bare form without also matching the
# "GB" of a capacity like "300GB".
GBPS_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(?:Gbps|Gb/s|G(?![Bb]))\b', re.I)
# SATA tops out at 6 Gbps, so a SATA listing claiming more is a mislabelled spec
# (12 Gbps is SAS). The drive is really 6 Gbps, so clamp rather than exclude.
SATA_MAX_GBPS = 6.0

# Multipacks would otherwise be read as one cheap drive at N times the real price.
LOT_RE = re.compile(r'\b(lot\s+of|bundle|\d+\s*x\s*\d|pack\s+of|qty\s*\d+)\b', re.I)
# SATA is also an SSD interface; these listings are not what this tracker is for.
SSD_RE = re.compile(r'\b(SSD|solid[\s-]state|NVMe|M\.2)\b', re.I)


def fetch_eur_rates():
    """ECB reference rates as {currency: units per EUR}. Empty dict on failure."""
    try:
        resp = requests.get(ECB_FX_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"Could not fetch ECB rates: {e}")
        return {}
    return {code: float(rate) for code, rate in FX_RE.findall(resp.text)}


def _to_eur(amount, watch, rates):
    """Currency conversion only. None when no rate is available."""
    code = watch.get("currency_code", "EUR")
    if code == "EUR":
        return amount
    rate = rates.get(code)
    return None if not rate else amount / rate  # ECB quotes units of `code` per 1 EUR


def to_eur_net(price, watch, rates):
    """Convert a scraped price to EUR excluding VAT. Returns None if unconvertible."""
    if watch.get("price_basis") == "gross":
        price = price / (1.0 + watch.get("vat_rate", 0.0))
    return _to_eur(price, watch, rates)


def shipping_to_eur_net(shipping, watch, rates):
    """Convert a published shipping charge to EUR excluding VAT.

    A shop quotes delivery on the same VAT basis as the prices its pages display, so
    display_vat_rate is what comes off here -- not price_basis/vat_rate, which describe
    the *scraped product field* and are a different basis on three of the four shops.
    """
    return _to_eur(shipping / (1.0 + watch.get("display_vat_rate", 0.0)), watch, rates)


def shipping_charged(price, watch, shipping):
    """Delivery actually payable on a one-drive order, honouring any free-over tier.

    The threshold is compared against the *net* scraped price: a Latvian buyer is
    invoiced ex-VAT on an intra-EU or export sale, and if that is wrong it errs towards
    charging delivery, which can only over-state a total -- never repeat the
    under-reporting this whole normalisation exists to prevent.
    """
    free_over = watch.get("shipping", {}).get("free_over")
    return 0.0 if free_over and price >= free_over else shipping


def landed_breakdown(price, watch, rates, shipping=0.0):
    """What one drive costs delivered to Latvia, itemised in EUR. None if unconvertible.

    The shop's own VAT rate is deliberately not used on the goods. An intra-EU purchase
    on a PVN number is reverse-charged, so German 19% never gets paid; a UK import is
    zero-rated for export and then charged LV import VAT at the border. Either way the
    rate that applies is Latvia's.

    Delivery sits *inside* the VAT base: VAT is due on the whole supply, and UK import
    VAT is levied on the customs value including freight.

    Each part is rounded to the cent and the total is summed *from those rounded parts*,
    because every alert prints all four -- a total a cent adrift from its own itemised
    breakdown reads as a bug. Note this figure is shown, not ranked on; see
    MAX_EUR_PER_TB.
    """
    goods = to_eur_net(price, watch, rates)
    delivery = shipping_to_eur_net(shipping_charged(price, watch, shipping), watch, rates)
    if goods is None or delivery is None:
        return None
    goods, delivery = round(goods, 2), round(delivery, 2)
    vat = round((goods + delivery) * LV_VAT_RATE, 2)
    return {"goods": goods, "delivery": delivery, "vat": vat,
            "total": round(goods + delivery + vat, 2)}


def to_eur_landed(price, watch, rates, shipping=0.0):
    """Just the delivered total from landed_breakdown(). None if unconvertible."""
    parts = landed_breakdown(price, watch, rates, shipping)
    return None if parts is None else parts["total"]


def shop_shelf_price(price, watch):
    """The number the shop's own page shows, in the shop's own currency.

    Carried on every alert purely so a deal can be reconciled against the page. Three
    of the four shops display gross at their local rate while the scraped field is net,
    which is what made alerts look wildly cheaper than the site.
    """
    return price * (1.0 + watch.get("display_vat_rate", 0.0))


# --- Delivery to Latvia -------------------------------------------------------------
# Every shop publishes a Latvia-applicable delivery price on a page a plain GET can
# read, so it is re-fetched each run rather than frozen into the source. Each watch
# still carries a `fallback`: if a page is redesigned, a stale-but-sane charge is far
# better than silently dropping delivery and under-reporting totals again.
#
# All four figures are per *order*, not per drive. Totals here assume a one-drive
# order, which is exact for buying one and pessimistic for buying several.


def _cells(table):
    """Row-major text cells of an HTML table, entities decoded."""
    return [[" ".join(html_lib.unescape(re.sub(r"<[^>]+>", "", c)).split())
             for c in re.findall(r"<t[dh][^>]*>.*?</t[dh]>", row, re.S)]
            for row in re.findall(r"<tr[^>]*>.*?</tr>", table, re.S)]


def _amount(text):
    """First money amount in a string, comma- or dot-decimal. None if there is none."""
    m = re.search(r"(\d[\d.,]*)", text or "")
    if not m:
        return None
    raw = m.group(1).rstrip(".,")
    # "17,99" and "1.234,56" are comma-decimal; "24.95" is dot-decimal.
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    return float(raw)


def shipping_techbuyer(text):
    """Techbuyer bands by order value, and Latvia falls under "Mainland Europe".

    The UK-only table sits above it on the same page with much cheaper numbers, so the
    search is anchored on the heading rather than taking the first table.
    """
    start = re.search(r"Mainland Europe", text, re.I)
    if not start:
        return None
    table = re.search(r"<table.*?</table>", text[start.end():], re.S)
    if not table:
        return None
    for row in _cells(table.group(0)):
        # The paying row is the one whose price cell is an amount, not "FREE".
        if len(row) >= 2 and re.search(r"up to", row[0], re.I):
            return _amount(row[1])
    return None


def shipping_renewtech(text):
    """Renewtech quotes one flat charge for every country inside the EU."""
    row = re.search(r"<tr[^>]*>(?:(?!</tr>).)*?(?:innerhalb der EU|within the EU)"
                    r"(?:(?!</tr>).)*?</tr>", text, re.S | re.I)
    if not row:
        return None
    # Cells read "2-3 Werktage / 20,- EUR"; the standard band is the first priced one.
    for cell in _cells(row.group(0))[0][1:]:
        if re.search(r"EUR|€", cell):
            return _amount(cell.split("/")[-1])
    return None


def shipping_servershop24(text):
    """One international flat rate covers every EU member state up to 31.5 kg.

    Anchored past the "DHL Standard International" heading so the much cheaper German
    domestic rate above it (and its free-over-400 tier, which Latvia does not get)
    cannot be picked up by mistake.
    """
    start = re.search(r"DHL Standard International", text, re.I)
    m = re.search(r"flat rate up to[^/]*/\s*(?:&euro;|€)?\s*([\d.,]+)",
                  text[start.end():] if start else "", re.I)
    return _amount(m.group(1)) if m else None


def shipping_gekko(text):
    """Gekko re-renders its whole delivery table server-side per ?countryCode=LV.

    The first table is the default carrier; later ones are alternatives that a buyer
    would have to choose deliberately, so the default is what gets quoted. A 3.5in
    drive is well under 1 kg, so the "From 0.00 kg" column is the one that applies.
    """
    for table in re.findall(r"<table.*?</table>", text, re.S):
        rows = _cells(table)
        if not rows or not any(re.search(r"From 0[.,]00", c) for c in rows[0]):
            continue
        col = next(i for i, c in enumerate(rows[0]) if re.search(r"From 0[.,]00", c))
        for row in rows[1:]:
            if len(row) > col and re.search(r"\d", row[col]):
                return _amount(row[col])
    return None


SHIPPING_PARSERS = {
    "techbuyer": shipping_techbuyer,
    "renewtech": shipping_renewtech,
    "servershop24": shipping_servershop24,
    "gekko": shipping_gekko,
}


def fetch_shipping(watch):
    """Delivery to Latvia for one shop, in that shop's own currency and VAT basis.

    Falls back to the configured constant on any failure. A silent zero here would put
    the tracker straight back to quoting less than the real cost, so the fallback is
    mandatory and every degradation is logged.
    """
    cfg = watch.get("shipping") or {}
    fallback = cfg.get("fallback", 0.0)
    parser = SHIPPING_PARSERS.get(cfg.get("source"))
    if not parser or not cfg.get("url"):
        return fallback
    try:
        resp = requests.get(cfg["url"], headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
        resp.encoding = "utf-8"
    except requests.RequestException as e:
        print(f"{watch['name']}: shipping page unreachable ({e}); using {fallback}")
        return fallback
    value = parser(resp.text)
    if value is None:
        print(f"{watch['name']}: could not read shipping from {cfg['url']}; "
              f"using fallback {fallback}")
        return fallback
    return value


def parse_ga4(html):
    """Techbuyer / Renewtech: Google Analytics view_item_list payload.

    The GA4 payload itself carries no stock field. Renewtech additionally emits a
    schema.org Product block per item, so stock is recovered from there by name;
    Techbuyer's category page has no such block, leaving in_stock as None (see
    product_in_stock(), which resolves those lazily for alert candidates only).
    """
    # Each product's availability is searched only up to where the *next* product
    # starts. A fixed-size window would both miss long blocks and, worse, let a
    # short block borrow the following product's stock state.
    products = list(LD_PRODUCT_RE.finditer(html))
    availability = {}
    for i, m in enumerate(products):
        end = products[i + 1].start() if i + 1 < len(products) else len(html)
        state = LD_AVAIL_RE.search(html, m.end(), end)
        if state:
            name = html_lib.unescape(m.group(1).encode().decode("unicode_escape", "ignore"))
            availability[name.strip().lower()] = state.group(1).lower() == "instock"

    items = []
    for blob in ITEM_RE.findall(html):
        try:
            item = json.loads(blob)
        except json.JSONDecodeError:
            continue
        item["in_stock"] = availability.get(item.get("item_name", "").strip().lower())
        items.append(item)
    return items


def parse_servershop24(html):
    slugs = {}
    for path, slug in SS24_HREF_RE.findall(html):
        slugs.setdefault(slug, "https://www.servershop24.de" + path)

    # Each stock badge sits after its product's <picture data-alt="NAME">, so walking
    # backwards from the badge to the nearest data-alt keys stock by name rather than
    # by DOM position -- position would silently mis-pair if the grid ever reorders.
    stock = {}
    for m in SS24_STOCK_RE.finditer(html):
        alt = None
        for a in SS24_ALT_RE.finditer(html, 0, m.start()):
            alt = a.group(1)
        if alt:
            stock[html_lib.unescape(alt).strip().lower()] = int(m.group(1))

    items = []
    for blob in SS24_RE.findall(html):
        try:
            raw = json.loads(blob)
        except json.JSONDecodeError:
            continue
        try:
            price = float(raw.get("price") or 0)
        except (TypeError, ValueError):
            continue
        name = raw.get("name", "")
        qty = stock.get(name.strip().lower())
        items.append({
            "item_id": raw.get("id"),
            "item_name": name,
            "item_brand": raw.get("brand"),
            "price": price,
            "url": slugs.get(raw.get("category")),
            "in_stock": None if qty is None else qty > 0,
            "stock_qty": qty,
        })
    return items


def parse_gekko(html):
    items = []
    for chunk in html.split('<article data-id="AL.Item"')[1:]:
        name = re.search(r'data-name="([^"]*)"', chunk)
        price = GEKKO_PRICE_RE.search(chunk)
        if not name or not price:
            continue
        sku = re.search(r'data-sku="([^";]*)', chunk)
        brand = re.search(r'data-brand="([^"]*)"', chunk)
        href = GEKKO_HREF_RE.search(chunk)
        avail = GEKKO_AVAIL_RE.search(chunk)
        qty = re.search(r'<div class="badge">(\d+)</div>', chunk)
        items.append({
            "item_id": sku.group(1) if sku else None,
            "item_name": html_lib.unescape(name.group(1)).replace("\\", ""),
            "item_brand": brand.group(1) if brand else None,
            "price": float(price.group(1)),
            "url": f"https://www.gekko-computer.de{href.group(1)}" if href else None,
            # Gekko colour-codes availability; only "green" means on the shelf now.
            "in_stock": None if not avail else avail.group(1) == "green",
            "stock_qty": int(qty.group(1)) if qty else None,
        })
    return items


def product_in_stock(url):
    """Read schema.org availability off a product page.

    Returns True/False, or None when the page or the field could not be read -- an
    unknown is never treated as out of stock, so a markup change degrades to the old
    behaviour of alerting rather than silently suppressing every deal.
    """
    if not url:
        return None
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"Could not check stock for {url}: {e}")
        return None
    m = LD_AVAIL_RE.search(resp.text)
    return None if not m else m.group(1).lower() == "instock"


PARSERS = {
    "ga4": parse_ga4,
    "servershop24": parse_servershop24,
    "gekko": parse_gekko,
}

# Each shop paginates with its own query parameter.
PAGE_PARAM = {
    "ga4": "p",
    "servershop24": "page",
    "gekko": "page",
}


def get_price(url):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            response = requests.get(url, headers=headers, timeout=15)
            soup = BeautifulSoup(response.text, "html.parser")
            price_tag = soup.select_one(".price .woocommerce-Price-amount")
            if price_tag:
                price_text = price_tag.get_text().strip()
                match = re.search(r'(\d+,\d+)', price_text)
                if match:
                    price_str = match.group(1).replace(',', '.')
                    return float(price_str)
        except requests.RequestException as e:
            print(f"Network error fetching {url} (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    return None


def fetch_page_items(url, page, source):
    """Return the product dicts embedded in one category page, or None on failure."""
    target = url if page == 1 else f"{url}?{PAGE_PARAM[source]}={page}"
    parser = PARSERS[source]
    for attempt in range(1, PAGE_RETRIES + 1):
        try:
            resp = requests.get(target, headers={"User-Agent": USER_AGENT}, timeout=30)
            resp.raise_for_status()
            return parser(resp.text)
        except requests.RequestException as e:
            print(f"Error fetching {target} (attempt {attempt}/{PAGE_RETRIES}): {e}")
            if attempt < PAGE_RETRIES:
                time.sleep(PAGE_RETRY_DELAY)
    return None


def fetch_category(url, max_pages, source):
    """Walk the paginated category and return every product, deduped by SKU."""
    items = {}
    page = 1
    while page <= max_pages:
        batch = fetch_page_items(url, page, source)
        if batch is None:
            # Network failure: keep what we have rather than losing the whole run.
            print(f"Giving up on {url} at page {page}")
            break
        if not batch:
            break
        new = 0
        for item in batch:
            sku = item.get("item_id")
            if sku and sku not in items:
                items[sku] = item
                new += 1
        if new == 0:
            break  # pagination wrapped around
        page += 1
        time.sleep(PAGE_DELAY)
    print(f"Scraped {len(items)} products from {url} ({page - 1} pages)")
    return list(items.values())


def fetch_watch(watch):
    """Scrape every category URL for a watch and merge them, deduped by SKU."""
    source = watch["source"]
    template = watch.get("item_url_template")
    merged = {}
    for url in watch["urls"]:
        for item in fetch_category(url, watch.get("max_pages", 80), source):
            sku = item.get("item_id")
            if sku and sku not in merged:
                if template and not item.get("url"):
                    item["url"] = template.format(id=sku)
                merged[sku] = item
    return list(merged.values())


def capacity_tb(name):
    """Pull drive capacity out of a product title. Returns TB, or None."""
    match = CAPACITY_RE.search(name)
    if not match:
        return None
    value, unit = float(match.group(1)), match.group(2).upper()
    return value if unit == "TB" else value / 1000.0


def transfer_gbps(name, cap=SATA_MAX_GBPS):
    """Interface speed from a product title, clamped to `cap`. None if unstated."""
    match = GBPS_RE.search(name)
    if not match:
        return None
    return min(float(match.group(1)), cap) if cap else float(match.group(1))


def rank_by_price_per_tb(items, watch, rates, shipping=0.0):
    """Filter to matching drives and sort them cheapest-per-TB first.

    `per_tb` stays in the shop's own currency and VAT basis for auditing; `eur_per_tb`
    is the delivered, 21%-VAT-inclusive figure everything is ranked and alerted on.
    `shipping` is that shop's delivery charge to Latvia, in its own currency.
    """
    pattern = re.compile(watch["name_filter"], re.I)
    min_tb = watch["min_tb"]
    min_gbps = watch.get("min_gbps")
    ranked = []
    for item in items:
        name = item.get("item_name", "")
        price = item.get("price")
        if not isinstance(price, (int, float)) or price <= 0:
            continue  # bundles and call-for-price items
        if not pattern.search(name):
            continue
        if SSD_RE.search(name) or LOT_RE.search(name):
            continue
        tb = capacity_tb(name)
        if not tb or tb < min_tb:
            continue
        gbps = transfer_gbps(name)
        if min_gbps:
            if gbps is None:
                # Most unstated drives are multi-TB SATA III that just omit the rate
                # -- including the cheapest drive on Techbuyer's whole listing -- so
                # dropping them here would throw away the best deals.
                if not watch.get("allow_unstated_gbps", True):
                    continue
            elif gbps < min_gbps:
                continue
        if item.get("in_stock") is False:
            continue  # None means "shop did not say"; only an explicit no is dropped
        parts = landed_breakdown(price, watch, rates, shipping)
        if parts is None:
            continue  # no FX rate available; better to skip than to alert wrongly
        eur_net, eur = parts["goods"], parts["total"]
        charged = shipping_charged(price, watch, shipping)  # native, for "shop shows"
        ranked.append({
            "sku": item.get("item_id"),
            "name": name,
            "brand": item.get("item_brand"),
            "price": round(price, 2),
            "tb": tb,
            "per_tb": round(price / tb, 2),
            # Goods only, before 21% VAT and before delivery. This is what the
            # threshold and the ranking are measured on; see MAX_EUR_PER_TB.
            "eur_net_price": eur_net,
            "eur_net_per_tb": round(eur_net / tb, 2),
            # ...and the same drive delivered, for information only. The three parts
            # below add up to eur_price exactly, so an alert can be checked by hand.
            "eur_delivery": parts["delivery"],
            "eur_vat": parts["vat"],
            "eur_price": eur,
            "eur_per_tb": round(eur / tb, 2),
            # What the shop's own pages show, for reconciling the alert against them.
            "shop_price": round(shop_shelf_price(price, watch), 2),
            "shop_shipping": round(charged, 2),
            "currency": watch["currency"],
            "gbps": gbps,
            "url": item.get("url"),
            "in_stock": item.get("in_stock"),
            "stock_qty": item.get("stock_qty"),
        })
    # Sorted on goods alone, matching the threshold. Delivery is flat per order, so
    # ranking on the delivered figure would sort by capacity as much as by value.
    ranked.sort(key=lambda d: d["eur_net_per_tb"])
    return ranked


def qualifies(deal, threshold):
    """Whether a deal clears the alert bar -- goods only, before VAT and delivery."""
    return deal["eur_net_per_tb"] <= threshold


def load_sitemap_index(sitemap_url=SITEMAP_URL):
    """SKU-ish URL slugs from the sitemap, so alerts can link to the product page."""
    try:
        resp = requests.get(sitemap_url, headers={"User-Agent": USER_AGENT}, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"Could not fetch sitemap: {e}")
        return []
    return [(re.sub(r"[^a-z0-9]", "", u.rsplit("/", 1)[-1]), u) for u in LOC_RE.findall(resp.text)]


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def renewtech_url(name):
    """Renewtech's grid is JS-rendered and its HTML carries no product anchors, but
    the slug is derivable from the title: everything before the ' - ' is the brand
    and model, which is exactly the URL. Verified 12/12 against live pages."""
    head = (name or "").split(" - ")[0].strip()
    return f"https://www.renewtech.de/{slugify(head)}.html" if head else None


# Shops whose product URLs have to be reconstructed rather than scraped.
URL_BUILDERS = {"renewtech": renewtech_url}


def url_exists(url):
    """HEAD a candidate link. Only ever called for deals going into a message."""
    try:
        resp = requests.head(url, headers={"User-Agent": USER_AGENT},
                             timeout=15, allow_redirects=True)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def resolve_urls(watch, deals, index):
    """Best product URL per SKU: scraped > sitemap > reconstructed > shop search."""
    builder = URL_BUILDERS.get(watch.get("url_builder"))
    search = watch.get("search_url")
    out = {}
    for deal in deals:
        url = deal.get("url") or find_product_url(index, deal["sku"])
        if not url and builder:
            candidate = builder(deal["name"])
            # A dead link in an alert is worse than a search page, so check first.
            if candidate and url_exists(candidate):
                url = candidate
        if not url and search:
            url = search.format(q=quote_plus(deal["name"].split(" - ")[0].strip()))
        out[deal["sku"]] = url
    return out


def find_product_url(index, sku):
    """Match 'HUH721010ALE600-HITACHI' to its product URL. Returns None if absent."""
    if not index or not sku:
        return None
    needle = re.sub(r"[^a-z0-9]", "", sku.rsplit("-", 1)[0].lower())
    if not needle:
        return None
    for slug, url in index:
        if needle in slug:
            return url
    return None


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram not configured: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID is missing")
        return
    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML",
              "disable_web_page_preview": True}
    )
    data = resp.json()
    if not data.get("ok"):
        print(f"Telegram error: {data}")


def load_state():
    if not os.path.exists(PRICE_LOG):
        return {"products": {}, "watches": {}}
    with open(PRICE_LOG, "r") as f:
        data = json.load(f)
    # Migrate the old flat {name: {...}} layout.
    if data and "products" not in data and "watches" not in data:
        data = {"products": data, "watches": {}}
    data.setdefault("products", {})
    data.setdefault("watches", {})
    return data


def save_state(state):
    with open(PRICE_LOG, "w") as f:
        json.dump(state, f, indent=2)


# Stamped on every history point. Bump this whenever the meaning of best_eur_per_tb
# changes, so the weekly trend never subtracts two figures on different bases -- the
# ex-VAT -> incl-21%-VAT switch would otherwise have read as a 21% overnight rise.
HISTORY_BASIS = "eur_incl_lv_vat"


def trend_baseline(history, week_ago):
    """Best €/TB from a week or more back, ignoring points on a superseded basis."""
    return next((h["best_eur_per_tb"] for h in reversed(history)
                 if h["date"] <= week_ago and "best_eur_per_tb" in h
                 and h.get("basis") == HISTORY_BASIS), None)


def is_weekly_run():
    # Sends weekly summary on Mondays
    return datetime.now().weekday() == 0


# One line, once per message -- not per deal, which is what made it unreadable.
EUR_NOTE = ("\n\n<i>Ranking and the alert threshold use the <b>before tax &amp; "
            "delivery</b> figure — goods only, EUR ex-VAT. The delivered line adds 21% "
            "Latvian VAT and delivery to Latvia, for information. A shop's own VAT rate "
            "never applies: EU orders are reverse-charged and UK is zero-rated for "
            "export, then charged LV import VAT at the border (plus a courier clearance "
            "fee, not counted here). Delivery is charged per <b>order</b>, so these "
            "totals assume one drive; buying several splits it. FX at ECB daily rates, "
            "re-read every run. \"shop shows\" is the shop's own page price, in its own "
            "currency and VAT, to check the alert against.</i>")


def format_deal(deal, url=None):
    """One deal: the delivered EUR total, plus the shop's own numbers to check it.

    The headline is always EUR delivered including 21% Latvian VAT, whatever the shop's
    local rate is -- that is what actually gets paid. The shop's own displayed price and
    delivery charge follow on their own line, in its own currency, because three of the
    four shops show gross at their local rate and the alert would otherwise look wildly
    cheaper than the page, which is exactly the confusion this format exists to end.
    """
    cur = deal.get("currency", "€")
    line = f"<b>{deal['name']}</b>"

    # The bar is measured on this figure, so it leads.
    if deal.get("eur_net_per_tb"):
        line += (f"\n    <b>€{deal['eur_net_per_tb']:.2f}/TB</b> before tax &amp; "
                 f"delivery — €{deal['eur_net_price']:.2f}")
    line += (f"\n    €{deal['eur_per_tb']:.2f}/TB delivered — "
             f"€{deal.get('eur_price', 0):.2f}")

    # Itemised, and the three parts add up to the delivered total exactly.
    if deal.get("eur_vat") is not None:
        delivery = (f"€{deal['eur_delivery']:.2f} delivery" if deal.get("eur_delivery")
                    else "free delivery")
        line += (f"\n      goods €{deal['eur_net_price']:.2f} + {delivery} "
                 f"+ 21% VAT €{deal['eur_vat']:.2f}")

    gbps = deal.get("gbps")
    line += f"\n    {deal['tb']:.4g}TB · " + (f"{gbps:.4g}Gbps" if gbps else "rate unstated")
    if deal.get("shop_price"):
        ship = deal.get("shop_shipping") or 0
        line += (f" · shop shows {cur}{deal['shop_price']:.2f}"
                 + (f" + {cur}{ship:.2f}" if ship else ""))
    url = url or deal.get("url")
    if url:
        line += f" · <a href='{url}'>view</a>"
    return line


def report_watch_failure(state, name, prev, reason, today, weekly, manual):
    """Alert on a shop going blind, then stay quiet until it recovers.

    A scheduled run has no one reading its log, so silence from a broken scraper is
    indistinguishable from silence meaning "no deals". This makes the difference
    audible without alerting every single day of a long outage.
    """
    failing_since = prev.get("failing_since") or today
    first_failure = not prev.get("failing_since")
    if first_failure:
        send_telegram(f"⚠️ <b>{name} has no usable data</b>\n\n"
                      f"{reason}.\n\nThis shop is no longer being tracked until it "
                      f"recovers. No news from it does not mean no deals.")
    elif weekly or manual:
        send_telegram(f"⚠️ <b>{name} still failing</b>\n\n"
                      f"{reason}.\n\nBlind since {failing_since}.")
    state["watches"][name] = {
        **prev,
        "last_checked": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "failing_since": failing_since,
        "last_error": reason,
    }


def check_watches(state, weekly, manual):
    # Re-pulled every run, so a GBP shop is always converted at the day's rate rather
    # than a stale one. Logged because a bad rate would skew every UK figure at once.
    rates = fetch_eur_rates()
    if not rates:
        print("Proceeding without FX rates: non-EUR watches will be skipped")
    else:
        used = {w.get("currency_code", "EUR") for w in WATCHES} - {"EUR"}
        print("ECB rates: " + ", ".join(f"{c}={rates.get(c)}" for c in sorted(used)))
    leaderboard = []

    for watch in WATCHES:
        name = watch["name"]
        threshold = MAX_EUR_PER_TB

        prev = state["watches"].get(name, {})
        today = datetime.now().strftime("%Y-%m-%d")

        try:
            items = fetch_watch(watch)
        except Exception as e:
            # One shop changing its markup must not take the other three down.
            print(f"{name} failed: {type(e).__name__}: {e}")
            report_watch_failure(state, name, prev, f"{type(e).__name__}: {e}",
                                 today, weekly, manual)
            continue

        # One request per shop per run, before ranking: delivery is inside every total.
        shipping = fetch_shipping(watch)
        print(f"{name}: delivery to Latvia {watch['currency']}{shipping:.2f}"
              + (f" (free over {watch['currency']}{watch['shipping']['free_over']:.0f})"
                 if watch.get("shipping", {}).get("free_over") else ""))

        ranked = rank_by_price_per_tb(items, watch, rates, shipping) if items else []
        if not ranked:
            reason = ("scrape returned no products" if not items
                      else f"{len(items)} product(s) scraped, none matched the filters")
            print(f"{name}: {reason}")
            report_watch_failure(state, name, prev, reason, today, weekly, manual)
            continue

        if prev.get("failing_since"):
            send_telegram(f"✅ <b>{name} recovered</b>\n\n"
                          f"Scraping again after failing since {prev['failing_since']}.")

        qualifying = [d for d in ranked if qualifies(d, threshold)]
        print(f"{name}: {len(ranked)} matched, {len(qualifying)} under "
              f"€{threshold:.2f}/TB before tax & delivery, "
              f"best €{ranked[0]['eur_net_per_tb']:.2f}/TB "
              f"(€{ranked[0]['eur_per_tb']:.2f}/TB delivered)")

        known = prev.get("deals", {})
        first_run = not prev

        new_deals, dropped = [], []
        for deal in qualifying:
            before = known.get(deal["sku"])
            if before is None:
                new_deals.append(deal)
            else:
                # Compared before tax and delivery, so a shop editing its delivery
                # table can never masquerade as a price drop.
                old = before.get("eur_net_per_tb")
                if old and (old - deal["eur_net_per_tb"]) / old >= PRICE_CHANGE_THRESHOLD:
                    dropped.append((deal, old))

        # Resolve product URLs only for the handful we actually put in a message --
        # the sitemap is ~2MB and matching is linear, so this stays off the daily path.
        # Shops whose listings carry their own product link skip this entirely.
        alerting = new_deals + [d for d, _ in dropped]
        need_urls = alerting + (ranked[:5] if (manual or weekly) else [])
        index = (load_sitemap_index(watch["sitemap"])
                 if need_urls and watch.get("sitemap") else [])
        urls = resolve_urls(watch, need_urls, index)

        # Shops that hide stock on the listing (Techbuyer) get a per-product check,
        # but only for what is about to be messaged -- a few requests, not a few
        # thousand. need_urls deliberately spans both the alert candidates and the
        # ranked[:5] behind the weekly report, manual summary and leaderboard:
        # checking only the alerts still let a sold-out drive be recommended there.
        if watch.get("verify_stock") and need_urls:
            sold_out, checked = set(), set()
            for deal in need_urls:
                if deal.get("in_stock") is not None or deal["sku"] in checked:
                    continue
                checked.add(deal["sku"])
                if product_in_stock(urls.get(deal["sku"]) or deal.get("url")) is False:
                    sold_out.add(deal["sku"])
                time.sleep(PAGE_DELAY)
            if sold_out:
                print(f"{name}: dropped {len(sold_out)} sold-out drive(s)")
                new_deals = [d for d in new_deals if d["sku"] not in sold_out]
                dropped = [(d, o) for d, o in dropped if d["sku"] not in sold_out]
                qualifying = [d for d in qualifying if d["sku"] not in sold_out]
                ranked = [d for d in ranked if d["sku"] not in sold_out]
                if not ranked:
                    print(f"{name}: everything ranked is sold out")
                    continue

        # The leaderboard formats deals without the urls dict in scope, so fold the
        # resolved link onto the deal itself or sitemap-linked shops lose theirs.
        for d in ranked[:5]:
            d["url"] = urls.get(d["sku"]) or d.get("url")
        leaderboard.extend((name, d) for d in ranked[:5])

        if first_run and qualifying:
            body = "\n\n".join(format_deal(d, urls.get(d["sku"])) for d in qualifying[:10])
            send_telegram(
                f"🆕 <b>{name} — now watching</b>\n\n"
                f"{len(qualifying)} drive(s) at or under €{threshold:.2f}/TB "
                f"before tax & delivery:\n\n{body}"
                + EUR_NOTE
            )
        else:
            if new_deals:
                body = "\n\n".join(format_deal(d, urls.get(d["sku"])) for d in new_deals[:10])
                send_telegram(
                    f"💰 <b>New deal under €{threshold:.2f}/TB before tax "
                    f"&amp; delivery</b>\n\n"
                    f"<i>{name}</i>\n\n{body}" + EUR_NOTE
                )
            for deal, old in dropped[:10]:
                pct = ((deal["eur_net_per_tb"] - old) / old) * 100
                send_telegram(
                    f"📉 <b>Price drop</b>\n\n<i>{name}</i>\n\n"
                    f"{format_deal(deal, urls.get(deal['sku']))}\n"
                    f"    was €{old:.2f}/TB ({pct:+.1f}%)"
                )

        if manual and not first_run:
            body = "\n\n".join(format_deal(d, urls.get(d["sku"])) for d in ranked[:5])
            send_telegram(
                f"ℹ️ <b>Manual check — {name}</b>\n\n"
                f"{len(ranked)} drives tracked, {len(qualifying)} under "
                f"€{threshold:.2f}/TB before tax &amp; delivery\n\nBest value right now:\n\n{body}" + EUR_NOTE
            )

        history = prev.get("history", [])
        history.append({"date": datetime.now().strftime("%Y-%m-%d"),
                        "best_eur_per_tb": ranked[0]["eur_net_per_tb"],
                        "matched": len(ranked),
                        "basis": HISTORY_BASIS})
        history = history[-30:]

        if weekly:
            week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            was = trend_baseline(history, week_ago)
            body = "\n\n".join(format_deal(d, urls.get(d["sku"])) for d in ranked[:5])
            trend = ""
            if was:
                diff = ranked[0]["eur_net_per_tb"] - was
                arrow = "📉" if diff < 0 else ("📈" if diff > 0 else "➡️")
                trend = (f"Best €/TB 7 days ago: €{was:.2f}\n"
                         f"Change: {arrow} {diff:+.2f}\n\n")
            send_telegram(
                f"📊 <b>Weekly Report — {name}</b>\n\n"
                f"{len(ranked)} drives tracked, {len(qualifying)} under "
                f"€{threshold:.2f}/TB before tax &amp; delivery\n{trend}"
                f"Best value right now:\n\n{body}" + EUR_NOTE
            )

        state["watches"][name] = {
            "last_checked": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "best_eur_per_tb": ranked[0]["eur_net_per_tb"],
            "matched": len(ranked),
            "threshold": threshold,
            # Only qualifying deals are remembered, so a drive that lapses above
            # the threshold and later returns will alert again.
            # Native price/per_tb and the shop's own shelf price are kept in state for
            # auditing, so a suspicious alert can be traced straight back to the page.
            "deals": {d["sku"]: {"name": d["name"], "price": d["price"], "tb": d["tb"],
                                 "per_tb": d["per_tb"], "eur_price": d["eur_price"],
                                 "eur_per_tb": d["eur_per_tb"],
                                 "eur_net_price": d.get("eur_net_price"),
                                 "eur_net_per_tb": d.get("eur_net_per_tb"),
                                 "eur_shipping": d.get("eur_shipping"),
                                 "shop_price": d.get("shop_price"),
                                 "shop_shipping": d.get("shop_shipping")}
                      for d in qualifying},
            "history": history,
        }

    if leaderboard and (weekly or manual):
        leaderboard.sort(key=lambda row: row[1]["eur_net_per_tb"])
        body = "\n\n".join(
            f"{format_deal(deal)}\n    <i>{shop}</i>"
            for shop, deal in leaderboard[:10]
        )
        send_telegram(
            f"🏆 <b>Best €/TB across all shops</b>\n\n"
            f"<i>{len(WATCHES)} shops compared</i>\n\n{body}" + EUR_NOTE
        )


def check_products(state, weekly, manual):
    prices = state["products"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    today = datetime.now().strftime("%Y-%m-%d")

    for product in PRODUCTS:
        name = product["name"]
        url = product["url"]
        currency = product.get("currency", "€")
        current_price = get_price(url)

        if current_price is None:
            print(f"Could not fetch price for {name}")
            if manual:
                send_telegram(
                    f"⚠️ <b>Manual Price Check Failed</b>\n\n"
                    f"Could not fetch price for <b>{name}</b>\n\n"
                    f"<a href='{url}'>View product</a>"
                )
            continue

        print(f"[{now}] {name}: {currency}{current_price}")

        data = prices.get(name, {})
        old_price = data.get("price")
        history = data.get("history", [])

        # Log today's price to history
        history.append({"date": today, "price": current_price})
        # Keep only last 30 days
        history = history[-30:]

        # Always notify on manual runs
        if manual:
            send_telegram(
                f"ℹ️ <b>Manual Price Check</b>\n\n"
                f"<b>{name}</b>\n"
                f"Current price: {currency}{current_price:.2f}\n\n"
                f"<a href='{url}'>View product</a>"
            )

        # Immediate alert on price change >= 5%
        if old_price and abs(current_price - old_price) / old_price >= PRICE_CHANGE_THRESHOLD:
            arrow = "📉" if current_price < old_price else "📈"
            diff = current_price - old_price
            pct = (diff / old_price) * 100
            send_telegram(
                f"{arrow} <b>Price change!</b>\n\n"
                f"<b>{name}</b>\n"
                f"Was: {currency}{old_price:.2f}\n"
                f"Now: {currency}{current_price:.2f}\n"
                f"Change: {diff:+.2f}{currency} ({pct:+.1f}%)\n\n"
                f"<a href='{url}'>View product</a>"
            )

        # Weekly summary every Monday
        if weekly:
            week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            week_old = next(
                (h["price"] for h in reversed(history) if h["date"] <= week_ago),
                old_price
            )
            weekly_diff = (current_price - week_old) if week_old else 0
            arrow = "📉" if weekly_diff < 0 else ("📈" if weekly_diff > 0 else "➡️")

            week_old_str = f"{currency}{week_old:.2f}" if week_old is not None else "N/A"
            send_telegram(
                f"📊 <b>Weekly Price Report</b>\n\n"
                f"<b>{name}</b>\n"
                f"Current price: {currency}{current_price:.2f}\n"
                f"7 days ago: {week_old_str}\n"
                f"Weekly change: {arrow} {weekly_diff:+.2f}{currency}\n\n"
                f"<a href='{url}'>View product</a>"
            )

        prices[name] = {
            "price": current_price,
            "last_checked": now,
            "history": history
        }


def main():
    state = load_state()
    weekly = is_weekly_run()
    manual = os.environ.get("MANUAL_RUN") == "true"

    check_products(state, weekly, manual)
    check_watches(state, weekly, manual)

    save_state(state)

    # Credentials used to be hardcoded, which meant a missing secret still looked
    # like a healthy run. Scraping is finished and saved by this point, so failing
    # here costs nothing but turns the Actions run red instead of a silent green
    # with every alert dropped on the floor.
    if not BOT_TOKEN or not CHAT_ID:
        missing = " and ".join(n for n, v in
                               (("TELEGRAM_TOKEN", BOT_TOKEN), ("TELEGRAM_CHAT_ID", CHAT_ID))
                               if not v)
        raise SystemExit(f"FAILED: {missing} not set -- prices were saved but no "
                         f"alert could be sent. Set it in the repository secrets.")


if __name__ == "__main__":
    main()
