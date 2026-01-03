# main.py (updated)

import time
import requests
import json
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, Filters

from bingx_client import BingxClient

# =====================================================
# ================== CONFIG ===========================
# =====================================================
Vol_period = 60
USERS_FILE = Path("users.json")

def load_users():
    if USERS_FILE.exists():
        return json.loads(USERS_FILE.read_text())
    return {}

def save_users(users):
    USERS_FILE.write_text(json.dumps(users, indent=4))

users = load_users()

BINANCE_FAPI_URL = "https://fapi.binance.com"

TELEGRAM_TOKEN = ""

CHECK_INTERVAL_MIN = 1

OI_4H_THRESHOLD = 10.0     # %
OI_24H_THRESHOLD = 16.0    # % 

PRICE_OI_RATIO = 0.5     # price_growth <= oi_growth * ratio
MIN_OI_USDT = 5_000_000  # фильтр мусора

SIGNAL_COOLDOWN_HOURS = 3  # защита от спама

REQUEST_TIMEOUT = 10

# =====================================================
# ================== INIT =============================
# =====================================================

bot = Bot(token=TELEGRAM_TOKEN)

# =====================================================
# ================== UTILS ============================
# =====================================================

def pct(now, past):
    if past == 0:
        return 0.0
    return (now - past) / past * 100.0

def send_alert(chat_id, text):
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

def get_oi_hist(symbol, limit):
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

def check_symbol(symbol):
    try:
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

        klines_4h = get_klines(symbol, 48)
        klines_24h = get_klines(symbol, 288)

        price_now = float(klines_4h[-1][4])
        price_4h_ago = float(klines_4h[0][4])
        price_24h_ago = float(klines_24h[0][4])

        price_growth_4h = pct(price_now, price_4h_ago)
        price_growth_24h = pct(price_now, price_24h_ago)

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

        # Process for each user
        for chat_id_str, user_data in list(users.items()):
            chat_id = int(chat_id_str)
            if not user_data.get("trading_enabled", False):
                # Still send alert if subscribed, even if trading disabled
                send_alert(chat_id, generate_alert_text(symbol, period, oi_growth_4h, oi_growth_24h, price_growth_4h, price_growth_24h, price_now, oi_now))
                continue

            last_signals = user_data.get("last_signal_time", {})
            if symbol in last_signals and datetime.utcnow() - datetime.fromisoformat(last_signals[symbol]) < timedelta(hours=SIGNAL_COOLDOWN_HOURS):
                continue

            # Update cooldown
            last_signals[symbol] = datetime.utcnow().isoformat()
            user_data["last_signal_time"] = last_signals
            save_users(users)

            # Send alert
            send_alert(chat_id, generate_alert_text(symbol, period, oi_growth_4h, oi_growth_24h, price_growth_4h, price_growth_24h, price_now, oi_now))

            # Open trade
            try:
                api_key = user_data["api_key"]
                api_secret = user_data["api_secret"]
                testnet = user_data.get("testnet", False)
                leverage = user_data.get("leverage", 10)
                margin_usdt = user_data.get("margin_usdt", 50)
                stop_loss_pct = user_data.get("stop_loss_pct", 2.0)
                take_profit_pct = user_data.get("take_profit_pct", 4.0)
                trailing_enabled = user_data.get("trailing_enabled", False)
                trailing_activation_pct = user_data.get("trailing_activation_pct", 1.5)
                trailing_rate_pct = round(user_data.get("trailing_rate_pct", 2) / 100, 3)

                bx = BingxClient(api_key, api_secret, testnet=testnet)
                if chat_id != 949808523:
                # Set leverage if needed (assuming client has method, add if not)
                    bx.set_leverage(symbol, 'long',leverage)  # Add this method if necessary

                s = symbol.replace('USDT', '-USDT')
                qty = (margin_usdt * leverage) / price_now

                stop_price = price_now * (1 - stop_loss_pct / 100)
                tp_price = price_now * (1 + take_profit_pct / 100)

                precision = bx.count_decimal_places(price_now)
                stop_price = round(stop_price, precision)
                tp_price = round(tp_price, precision)
                qty = round(qty, 0 if precision < 2 else 1)  # Adjust as per your logic
                pos_side_BOTH = True if chat_id == 949808523 else False
                if symbol in user_data.get("blacklist", []):
                    continue

                # === VOLUME FILTER ===
                if user_data.get("volume_filter_enabled", False):
                    multiplier = user_data.get("volume_multiplier", 2.0)
                    if not check_volume_filter(symbol, multiplier):
                        continue
                    
                resp = bx.place_market_order('long', qty, s, stop_price, tp_price, pos_side_BOTH)
                print(f"Order placed for {chat_id} on {symbol}: {resp}")

                if trailing_enabled:
                    activation_price = price_now * (1 + trailing_activation_pct / 100)
                    resp_trail = bx.set_trailing(s, 'long', qty, activation_price, trailing_rate_pct)
                    print(f"Trailing set for {chat_id} on {symbol}: {resp_trail}")

            except Exception as e:
                print(f"Trade error for {chat_id} on {symbol}: {e}")
                send_alert(chat_id, f"Ошибка открытия сделки на {symbol}: {str(e)}")

    except Exception as e:
        print(f"{symbol}: {e}")

def generate_alert_text(symbol, period, oi_growth_4h, oi_growth_24h, price_growth_4h, price_growth_24h, price_now, oi_now):
    return (
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

# =====================================================
# ================== TELEGRAM HANDLERS ================
# =====================================================

# Conversation states
(
    API_KEY, API_SECRET, TESTNET, LEVERAGE, MARGIN, STOP_LOSS, TAKE_PROFIT,
    TRAILING_ENABLED, TRAILING_ACTIVATION, TRAILING_RATE, TRADING_ENABLED,
    VOLUME_MULTIPLIER
) = range(12)

def check_volume_filter(symbol, multiplier):
    klines = get_klines(symbol, Vol_period)

    if len(klines) < Vol_period:
        return False

    volumes = [float(k[5]) for k in klines[:-1]]  # без текущей
    avg_volume = sum(volumes) / len(volumes)

    current_volume = float(klines[-1][5])

    return current_volume >= avg_volume * multiplier

def start(update: Update, context):
    chat_id = str(update.effective_chat.id)
    if chat_id not in users:
        users[chat_id] = {
            "trading_enabled": False,
            "testnet": False,
            "api_key": "",
            "api_secret": "",
            "leverage": 10,
            "margin_usdt": 50,
            "stop_loss_pct": 2.0,
            "take_profit_pct": 4.0,
            "trailing_enabled": False,
            "trailing_activation_pct": 1.5,
            "trailing_rate_pct": 0.5,
            "last_signal_time": {},

            # === NEW ===
            "volume_filter_enabled": False,
            "volume_multiplier": 2.0,
            "blacklist": []
        }
        save_users(users)
    update.message.reply_text("✅ Подписка на OI-сигналы активирована. Используйте /settings для настроек.")
    return show_settings_menu(update, context)

def stop(update: Update, context):
    chat_id = str(update.effective_chat.id)
    if chat_id in users:
        del users[chat_id]
        save_users(users)
    update.message.reply_text("❌ Подписка отключена")
    return ConversationHandler.END

def settings(update: Update, context):
    return show_settings_menu(update, context)

def show_settings_menu(update: Update, context):
    if update.callback_query:
        chat_id = str(update.callback_query.message.chat_id)
    else:
        chat_id = str(update.effective_chat.id)
    
    user = users.get(chat_id, {
        "trading_enabled": False, "testnet": False, "api_key": "", "api_secret": "",
        "leverage": 10, "margin_usdt": 50, "stop_loss_pct": 2.0, "take_profit_pct": 4.0,
        "trailing_enabled": False, "trailing_activation_pct": 1.5, "trailing_rate_pct": 0.5
    })

    keyboard = [
        [InlineKeyboardButton(f"Торговля: {'✅ Вкл' if user.get('trading_enabled') else '❌ Выкл'}", callback_data='toggle_trading')],
        [InlineKeyboardButton(f"API Key: {'✅ Установлен' if user.get('api_key') else '❌ Не установлен'}", callback_data='set_api_key')],
        [InlineKeyboardButton(f"API Secret: {'✅ Установлен' if user.get('api_secret') else '❌ Не установлен'}", callback_data='set_api_secret')],
        [InlineKeyboardButton(f"Сеть: {'Testnet' if user.get('testnet') else 'Real'}", callback_data='toggle_testnet')],
        [InlineKeyboardButton(f"Плечо: {user.get('leverage', 10)}x", callback_data='set_leverage')],
        [InlineKeyboardButton(f"Маржа: {user.get('margin_usdt', 50)} USDT", callback_data='set_margin')],
        [InlineKeyboardButton(f"SL: {user.get('stop_loss_pct', 2.0)}%", callback_data='set_sl')],
        [InlineKeyboardButton(f"TP: {user.get('take_profit_pct', 4.0)}%", callback_data='set_tp')],
        [InlineKeyboardButton(f"Трейлинг: {'✅ Вкл' if user.get('trailing_enabled') else '❌ Выкл'}", callback_data='toggle_trailing')],
        [InlineKeyboardButton(f"Активация трейлинга: {user.get('trailing_activation_pct', 1.5)}%", callback_data='set_trail_act')],
        [InlineKeyboardButton(f"Price Rate: {user.get('trailing_rate_pct', 0.5)}%", callback_data='set_trail_rate')],
        [InlineKeyboardButton(
            f"Volume filter: {'✅ Вкл' if user.get('volume_filter_enabled') else '❌ Выкл'}",
            callback_data='toggle_volume_filter'
        )],
        [InlineKeyboardButton(
            f"Volume x{user.get('volume_multiplier', 2.0)}",
            callback_data='set_volume_multiplier'
        )],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "<b>⚙️ Настройки торгового бота</b>"

    if update.callback_query:
        try:
            update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
        except Exception as e:
            if "Message is not modified" in str(e):
                pass  # игнорируем эту ошибку
            else:
                raise
    else:
        update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")
    
    return ConversationHandler.END
def blacklist_show(update: Update, context):
    chat_id = str(update.effective_chat.id)
    blacklist = users.get(chat_id, {}).get("blacklist", [])

    if not blacklist:
        update.message.reply_text("📭 Чёрный список пуст")
        return

    text = "<b>⛔ Чёрный список:</b>\n\n"
    text += "\n".join(f"• {s}" for s in sorted(blacklist))

    update.message.reply_text(text, parse_mode="HTML")
def button_handler(update: Update, context):
    query = update.callback_query
    query.answer()  # Обязательно отвечаем на callback!
    chat_id = str(query.message.chat_id)
    data = query.data

    if data == 'toggle_trading':
        users[chat_id]['trading_enabled'] = not users[chat_id].get('trading_enabled', False)
        save_users(users)

    elif data == 'toggle_testnet':
        users[chat_id]['testnet'] = not users[chat_id].get('testnet', False)
        save_users(users)

    elif data == 'toggle_trailing':
        users[chat_id]['trailing_enabled'] = not users[chat_id].get('trailing_enabled', False)
        save_users(users)
    elif data == 'set_volume_multiplier':
        context.user_data['setting'] = 'set_volume_multiplier'
        query.edit_message_text("Введите volume multiplier (например 2.0):")
        return VOLUME_MULTIPLIER
    elif data.startswith('set_'):
        context.user_data['setting'] = data
        field_name = data.replace('set_', '').replace('_', ' ').title()
        query.edit_message_text(f"Введите новое значение для <b>{field_name}</b>:", parse_mode="HTML")
        return get_state(data)
    elif data == 'toggle_volume_filter':
        users[chat_id]['volume_filter_enabled'] = not users[chat_id].get('volume_filter_enabled', False)
        save_users(users)

    
    # Если мы здесь — значит, была toggle-операция, обновляем меню
    show_settings_menu(update, context)
    return ConversationHandler.END

def get_state(data):
    map = {
        'set_api_key': API_KEY,
        'set_api_secret': API_SECRET,
        'set_leverage': LEVERAGE,
        'set_margin': MARGIN,
        'set_sl': STOP_LOSS,
        'set_tp': TAKE_PROFIT,
        'set_trail_act': TRAILING_ACTIVATION,
        'set_trail_rate': TRAILING_RATE,
        'set_volume_multiplier': VOLUME_MULTIPLIER,  # ← ВАЖНО
    }
    return map.get(data, ConversationHandler.END)

def set_api_key(update: Update, context):
    return set_value(update, context, 'api_key', str)

def set_api_secret(update: Update, context):
    return set_value(update, context, 'api_secret', str)

def set_leverage(update: Update, context):
    return set_value(update, context, 'leverage', int)

def set_margin(update: Update, context):
    return set_value(update, context, 'margin_usdt', float)

def set_sl(update: Update, context):
    return set_value(update, context, 'stop_loss_pct', float)

def set_tp(update: Update, context):
    return set_value(update, context, 'take_profit_pct', float)

def set_trail_act(update: Update, context):
    return set_value(update, context, 'trailing_activation_pct', float)

def set_trail_rate(update: Update, context):
    return set_value(update, context, 'trailing_rate_pct', float)

def set_volume_multiplier(update: Update, context):
    return set_value(update, context, 'volume_multiplier', float)

def blacklist_add(update: Update, context):
    chat_id = str(update.effective_chat.id)

    if not context.args:
        update.message.reply_text("Использование: /blacklist_add BTCUSDT")
        return

    symbol = context.args[0].upper()

    users[chat_id].setdefault("blacklist", [])
    if symbol not in users[chat_id]["blacklist"]:
        users[chat_id]["blacklist"].append(symbol)
        save_users(users)

    update.message.reply_text(f"⛔ {symbol} добавлен в чёрный список")

def blacklist_remove(update: Update, context):
    chat_id = str(update.effective_chat.id)

    if not context.args:
        update.message.reply_text("Использование: /blacklist_remove BTCUSDT")
        return

    symbol = context.args[0].upper()

    if symbol in users[chat_id].get("blacklist", []):
        users[chat_id]["blacklist"].remove(symbol)
        save_users(users)

    update.message.reply_text(f"✅ {symbol} удалён из чёрного списка")
    
    
def set_value(update: Update, context, key, type_func=str):
    chat_id = str(update.effective_chat.id)
    text = update.message.text.strip()
    
    try:
        if type_func == bool:
            value = text.lower() in ['true', '1', 'yes', 'да', 'вкл']
        else:
            value = type_func(text)
        users[chat_id][key] = value
        save_users(users)
        update.message.reply_text(f"✅ {key.replace('_', ' ').title()} установлен: {value}")
    except ValueError:
        update.message.reply_text("❌ Неверный формат. Попробуйте снова.")
        # Остаёмся в текущем состоянии ввода
        return get_state(context.user_data['setting'])
    
    # Успешно — выходим из ввода и показываем меню
    show_settings_menu(update, context)
    return ConversationHandler.END

# =====================================================
# ================== MAIN LOOP ========================
# =====================================================

def telegram_bot():
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CommandHandler('settings', settings),
            CallbackQueryHandler(button_handler)
        ],
        states={
            API_KEY: [MessageHandler(Filters.text & ~Filters.command, set_api_key)],
            API_SECRET: [MessageHandler(Filters.text & ~Filters.command, set_api_secret)],
            LEVERAGE: [MessageHandler(Filters.text & ~Filters.command, set_leverage)],
            MARGIN: [MessageHandler(Filters.text & ~Filters.command, set_margin)],
            STOP_LOSS: [MessageHandler(Filters.text & ~Filters.command, set_sl)],
            TAKE_PROFIT: [MessageHandler(Filters.text & ~Filters.command, set_tp)],
            TRAILING_ACTIVATION: [MessageHandler(Filters.text & ~Filters.command, set_trail_act)],
            TRAILING_RATE: [MessageHandler(Filters.text & ~Filters.command, set_trail_rate)],
            VOLUME_MULTIPLIER: [MessageHandler(Filters.text & ~Filters.command, set_volume_multiplier)]
        },
        fallbacks=[],
    )
    dp.add_handler(CommandHandler("blacklist_add", blacklist_add))
    dp.add_handler(CommandHandler("blacklist_show", blacklist_show))
    dp.add_handler(CommandHandler("blacklist_remove", blacklist_remove))
    dp.add_handler(conv_handler)
    dp.add_handler(CommandHandler("stop", stop))

    updater.start_polling()

import threading
threading.Thread(target=telegram_bot, daemon=True).start()

def main():
    symbols = get_symbols()
    print(f"[INFO] Symbols loaded: {len(symbols)}")

    while True:
        start_time = time.time()
        print(f"[INFO] Scan started {datetime.utcnow()}")

        for symbol in symbols:
            check_symbol(symbol)
            time.sleep(0.15)  # rate limit protection

        elapsed = time.time() - start_time
        sleep_time = max(60, CHECK_INTERVAL_MIN * 60 - elapsed)
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()