import time
import requests
from datetime import datetime
from collections import defaultdict

from bingx_client import BingxClient
APIKEY = ''
APISECRET = ''
bx = BingxClient(APIKEY, APISECRET)
# =====================================================
# ================== CONFIG ===========================
# =====================================================
import json
from pathlib import Path

USERS_FILE = Path("users.json")

def load_users():
    if USERS_FILE.exists():
        return set(json.loads(USERS_FILE.read_text()))
    return set()

def save_users(users):
    USERS_FILE.write_text(json.dumps(list(users)))

users = load_users()


BINANCE_FAPI_URL = "https://fapi.binance.com"

TELEGRAM_TOKEN = ""
TELEGRAM_CHAT_ID = ""

CHECK_INTERVAL_MIN = 3

OI_4H_THRESHOLD = 8.0     # %
OI_24H_THRESHOLD = 12.0    # % 

PRICE_OI_RATIO = 0.4     # price_growth <= oi_growth * ratio
MIN_OI_USDT = 5_000_000  # фильтр мусора

SIGNAL_COOLDOWN_HOURS = 3  # защита от спама

REQUEST_TIMEOUT = 5

# =====================================================
# ================== INIT =============================
# =====================================================

import telebot

bot = telebot.TeleBot(TELEGRAM_TOKEN)
last_signal_time = defaultdict(lambda: datetime.min)

# =====================================================
# ================== UTILS ============================
# =====================================================

def pct(now, past):
    if past == 0:
        return 0.0
    return (now - past) / past * 100.0

def send_alert(text):
    for chat_id in users:
        try:
            bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML"
            )
        except Exception as e:
            print(f"Telegram error {chat_id}: {e}")


def binance_get(endpoint, params=None):
    url = BINANCE_FAPI_URL + endpoint
    r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

# =====================================================
# ================== DATA =============================
# =====================================================

def get_symbols():
    data = binance_get("/fapi/v1/exchangeInfo")
    return [
        s["symbol"]
        for s in data["symbols"]
        if s["contractType"] == "PERPETUAL"
        and s["quoteAsset"] == "USDT"
        and s["status"] == "TRADING"
    ]


def get_mark_price(symbol):
    data = binance_get("/fapi/v1/premiumIndex", {"symbol": symbol})
    return float(data["markPrice"])


def get_oi_hist(symbol, limit):
    """
    5m OI history
    limit=48  -> 4h
    limit=288 -> 24h
    """
    return binance_get(
        "/futures/data/openInterestHist",
        {
            "symbol": symbol,
            "period": "5m",
            "limit": limit
        }
    )


def get_klines(symbol, limit):
    return binance_get(
        "/fapi/v1/klines",
        {
            "symbol": symbol,
            "interval": "5m",
            "limit": limit
        }
    )

# =====================================================
# ================== CORE LOGIC =======================
# =====================================================
@bot.message_handler(commands=['start'])
def start_handler(message):
    chat_id = message.chat.id
    users.add(chat_id)
    save_users(users)
    bot.send_message(chat_id, "✅ Подписка на OI-сигналы активирована")


@bot.message_handler(commands=['stop'])
def stop_handler(message):
    chat_id = message.chat.id
    users.discard(chat_id)
    save_users(users)
    bot.send_message(chat_id, "❌ Подписка отключена")
    
def check_symbol(symbol):
    try:
        # ---- Cooldown ----
        if datetime.utcnow() - last_signal_time[symbol] < timedelta(hours=SIGNAL_COOLDOWN_HOURS):
            return

        # ---- OI ----
        oi_4h = get_oi_hist(symbol, 48)
        oi_24h = get_oi_hist(symbol, 288)

        if len(oi_24h) < 288:
            return

        oi_now = float(oi_4h[-1]["sumOpenInterestValue"])
        oi_4h_ago = float(oi_4h[0]["sumOpenInterestValue"])
        oi_24h_ago = float(oi_24h[0]["sumOpenInterestValue"])
        
        if oi_now < MIN_OI_USDT:
            return

        oi_growth_4h = pct(oi_now, oi_4h_ago)
        oi_growth_24h = pct(oi_now, oi_24h_ago)

        # ---- PRICE ----
        klines_4h = get_klines(symbol, 48)
        klines_24h = get_klines(symbol, 288)

        price_now = float(klines_4h[-1][4])
        price_4h_ago = float(klines_4h[0][4])
        price_24h_ago = float(klines_24h[0][4])

        price_growth_4h = pct(price_now, price_4h_ago)
        price_growth_24h = pct(price_now, price_24h_ago)

        print(symbol, oi_growth_24h, oi_growth_4h, '\n', price_growth_24h, price_growth_4h)
        # ---- CONDITIONS ----
        signal_4h = (
            oi_growth_4h >= OI_4H_THRESHOLD and
            price_growth_4h <= oi_growth_4h * PRICE_OI_RATIO
        )

        signal_24h = (
            oi_growth_24h >= OI_24H_THRESHOLD and
            price_growth_24h <= oi_growth_24h * PRICE_OI_RATIO
        )

        if not (signal_4h or signal_24h):
            return

        period = "4h" if signal_4h else "24h"

        last_signal_time[symbol] = datetime.utcnow()

        # ---- ALERT ----
        send_alert(
            f"<b>${symbol.replace('USDT', '')}</b>\n"
            f"🚨 <b>OI ALERT</b>\n"
            f"⏱ Период: {period}\n\n"
            f"OI 4h: {oi_growth_4h:.1f}%\n"
            f"OI 24h: {oi_growth_24h:.1f}%\n\n"
            f"Цена 4h: {price_growth_4h:.1f}%\n"
            f"Цена 24h: {price_growth_24h:.1f}%\n\n"
            f"Текущая цена: {price_now:.4f}\n"
            f"OI: {oi_now/1e6:.1f}M USDT\n\n"
            f"<i>OI растёт быстрее цены → возможное накопление</i>"
        )
        
    except Exception as e:
        print(f"{symbol}: {e}")

# =====================================================
# ================== MAIN LOOP ========================
# =====================================================
import threading

def telegram_bot():
    

    bot.infinity_polling()

threading.Thread(target=telegram_bot, daemon=True).start()

def main():
    symbols = get_symbols()
    print(f"[INFO] Symbols loaded: {len(symbols)}")

    while True:
        start = time.time()
        print(f"[INFO] Scan started {datetime.utcnow()}")

        for symbol in symbols:
            check_symbol(symbol)
            time.sleep(0.15)  # защита от rate limit

        elapsed = time.time() - start
        sleep_time = max(60, CHECK_INTERVAL_MIN * 60 - elapsed)
        time.sleep(sleep_time)


if __name__ == "__main__":
    from datetime import timedelta
    main()
