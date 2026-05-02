import requests
import json
import os
import re
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

PRODUCTS = [
    {
        "name": "Raspberry Pi 5 - 16 GB",
        "url": "https://raspberrypi.dk/en/product/raspberry-pi-5-16-gb/",
    }
]

PRICE_LOG = "prices.json"

def get_price(url):
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
        print(f"Network error fetching {url}: {e}")
    return None

def send_telegram(message):
    if BOT_TOKEN and CHAT_ID:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
        )

def load_prices():
    if not os.path.exists(PRICE_LOG):
        return {}
    with open(PRICE_LOG, "r") as f:
        return json.load(f)

def save_prices(prices):
    with open(PRICE_LOG, "w") as f:
        json.dump(prices, f, indent=2)

def is_weekly_run():
    # Sends weekly summary on Mondays
    return datetime.now().weekday() == 0

def check_prices():
    prices = load_prices()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    today = datetime.now().strftime("%Y-%m-%d")
    weekly = is_weekly_run()

    for product in PRODUCTS:
        name = product["name"]
        url = product["url"]
        current_price = get_price(url)

        if current_price is None:
            print(f"Could not fetch price for {name}")
            continue

        print(f"[{now}] {name}: €{current_price}")

        data = prices.get(name, {})
        old_price = data.get("price")
        history = data.get("history", [])

        # Log today's price to history
        history.append({"date": today, "price": current_price})
        # Keep only last 30 days
        history = history[-30:]

        # Always notify on manual runs
        if os.environ.get("MANUAL_RUN") == "true":
            send_telegram(
                f"ℹ️ <b>Manual Price Check</b>\n\n"
                f"<b>{name}</b>\n"
                f"Current price: €{current_price:.2f}\n\n"
                f"<a href='{url}'>View product</a>"
            )

        # Immediate alert on price change
        if old_price and current_price != old_price:
            arrow = "📉" if current_price < old_price else "📈"
            diff = current_price - old_price
            send_telegram(
                f"{arrow} <b>Price change!</b>\n\n"
                f"<b>{name}</b>\n"
                f"Was: €{old_price:.2f}\n"
                f"Now: €{current_price:.2f}\n"
                f"Change: {diff:+.2f}€\n\n"
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

            week_old_str = f"€{week_old:.2f}" if week_old is not None else "N/A"
            send_telegram(
                f"📊 <b>Weekly Price Report</b>\n\n"
                f"<b>{name}</b>\n"
                f"Current price: €{current_price:.2f}\n"
                f"7 days ago: {week_old_str}\n"
                f"Weekly change: {arrow} {weekly_diff:+.2f}€\n\n"
                f"<a href='{url}'>View product</a>"
            )

        prices[name] = {
            "price": current_price,
            "last_checked": now,
            "history": history
        }

    save_prices(prices)

if __name__ == "__main__":
    check_prices()
