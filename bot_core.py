import os
import sqlite3
import logging
import json
from datetime import datetime, timedelta
import requests
import ccxt
import pandas as pd

# ---------- Конфигурация ----------
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("BOT_TOKEN not set in environment")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip()] if ADMIN_IDS_STR else []

MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET = os.getenv("MEXC_SECRET")

# ---------- Настройки сканера ----------
SPREAD_THRESHOLD = 0.1
MIN_VOLUME = 1000
BLACKLIST = ['USDC/USDT', 'USDD/USDT', 'BUSD/USDT', 'DAI/USDT', 'TUSD/USDT', 'FDUSD/USDT', 'X/USDT']

# ---------- Биржи и шаблоны ссылок (не используются в сообщениях, но оставлены для совместимости) ----------
EXCHANGES = {
    'kucoin': {
        'inst': ccxt.kucoin({'enableRateLimit': True}),
        'url': 'https://www.kucoin.com/trade/{}-USDT'
    },
    'mexc': {
        'inst': ccxt.mexc({
            'apiKey': MEXC_API_KEY,
            'secret': MEXC_SECRET,
            'enableRateLimit': True
        }),
        'url': 'https://www.mexc.com/ru-RU/exchange/{}_USDT?_from=header'
    },
}

# ---------- База данных ----------
DB_PATH = "subscriptions.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                 (user_id INTEGER PRIMARY KEY, username TEXT, expires DATE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS auto_scan
                 (user_id INTEGER PRIMARY KEY, interval INTEGER, last_sent TIMESTAMP)''')
    conn.commit()
    conn.close()

init_db()

# ---------- Подписки ----------
def is_subscribed(user_id):
    if user_id in ADMIN_IDS:
        return True
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT expires FROM subscriptions WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        expires = datetime.strptime(row[0], "%Y-%m-%d").date()
        if expires >= datetime.now().date():
            return True
    return False

def grant_access(user_id, username, days=30):
    expires = (datetime.now() + timedelta(days=days)).date()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("REPLACE INTO subscriptions (user_id, username, expires) VALUES (?, ?, ?)",
              (user_id, username, expires.isoformat()))
    conn.commit()
    conn.close()
    return expires

def get_expiry_date(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT expires FROM subscriptions WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def enable_auto_scan(user_id, interval):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("REPLACE INTO auto_scan (user_id, interval, last_sent) VALUES (?, ?, ?)",
              (user_id, interval, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def disable_auto_scan(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM auto_scan WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def get_auto_scan_users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, interval, last_sent FROM auto_scan")
    rows = c.fetchall()
    conn.close()
    return rows

# ---------- Отправка сообщений ----------
def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    url = f"{TELEGRAM_API_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        logging.error(f"Failed to send message to {chat_id}: {e}")

def send_message_with_keyboard(chat_id, text, keyboard_buttons):
    markup = {
        "keyboard": keyboard_buttons,
        "resize_keyboard": True,
        "one_time_keyboard": False
    }
    send_message(chat_id, text, reply_markup=markup)

# ---------- Клавиатуры ----------
def get_main_keyboard():
    return [
        ["🚀 Найти арбитраж (сейчас)"],
        ["💰 Купить подписку 2000₽/мес"],
        ["▶️ Авто-скан (1 мин)", "▶️ Авто-скан (5 мин)"],
        ["⏹ Стоп сканирование"],
        ["📊 Статус"]
    ]

def get_payment_keyboard():
    sbp_link = "https://www.sberbank.ru/ru/choise_bank?requisiteNumber=79093545631&bankCode=100000000111"
    return {
        "inline_keyboard": [
            [{"text": "💳 Оплатить через СБП", "url": sbp_link}],
            [{"text": "✅ Я оплатил (проверить)", "callback_data": "check_payment"}]
        ]
    }

# ---------- Функции для работы с сетями ----------
def normalize_network(name):
    name = str(name).upper().replace("-", "").replace("_", "").replace(" ", "")
    synonyms = {
        "TRC20": "TRC20", "TRX": "TRC20", "TRON": "TRC20",
        "ERC20": "ERC20", "ETH": "ERC20", "ETHEREUM": "ERC20", "ETHERC20": "ERC20",
        "BEP20": "BEP20", "BSC": "BEP20", "BNBSMART": "BEP20",
        "SOL": "SOL", "SOLANA": "SOL", "SPL": "SOL",
        "MATIC": "MATIC", "POLYGON": "MATIC",
        "ARB": "ARB", "ARBITRUM": "ARB", "ARBITRUMONE": "ARB",
        "OP": "OP", "OPTIMISM": "OP",
        "BASE": "BASE",
        "KCC": "KCC", "KUCOIN": "KCC",
    }
    return synonyms.get(name, name)

def fetch_network_data(exchange_name, exchange):
    logging.info(f"⏳ Загрузка сетей с {exchange_name}...")
    networks = {}
    try:
        if hasattr(exchange, 'fetch_currencies'):
            currencies = exchange.fetch_currencies()
            for code, data in currencies.items():
                if 'networks' in data and data['networks']:
                    first_network = list(data['networks'].keys())[0]
                    networks[code] = normalize_network(first_network)
        else:
            markets = exchange.load_markets()
            for symbol, market in markets.items():
                if not symbol.endswith('/USDT'):
                    continue
                base = symbol.split('/')[0]
                if 'info' in market and 'networks' in market['info']:
                    net_list = market['info']['networks']
                    if net_list:
                        networks[base] = normalize_network(net_list[0].get('network', ''))
        logging.info(f"✅ Загружено {len(networks)} сетей с {exchange_name}")
    except Exception as e:
        logging.error(f"Ошибка загрузки сетей с {exchange_name}: {e}")
    return networks

def update_network_data():
    global currency_data
    all_networks = {}
    for name, exch in EXCHANGES.items():
        networks = fetch_network_data(name, exch['inst'])
        for base, net in networks.items():
            if base not in all_networks:
                all_networks[base] = {}
            all_networks[base][name] = net
    currency_data = all_networks
    logging.info(f"📦 Всего активов с данными сетей: {len(currency_data)}")

currency_data = {}
update_network_data()

def check_transfer_possible(buy_ex, sell_ex, symbol):
    if not currency_data:
        return True, "?"
    base = symbol.split('/')[0]
    if base not in currency_data:
        return False, None
    buy_net = currency_data[base].get(buy_ex)
    sell_net = currency_data[base].get(sell_ex)
    if buy_net and sell_net and buy_net == sell_net:
        return True, buy_net
    return False, None

# ---------- Получение цен с бирж ----------
def fetch_prices(exchange_name, exchange):
    prices = {}
    try:
        tickers = exchange.fetch_tickers()
        for symbol, ticker in tickers.items():
            if not symbol.endswith('/USDT'):
                continue
            if symbol in BLACKLIST:
                continue
            last = ticker.get('last')
            quote_volume = ticker.get('quoteVolume')
            if last and quote_volume and quote_volume >= MIN_VOLUME:
                prices[symbol] = {
                    'price': last,
                    'volume': quote_volume
                }
    except Exception as e:
        logging.error(f"Ошибка получения тикеров с {exchange_name}: {e}")
    return prices

# ---------- Основной сканер ----------
def scan_market():
    opportunities = []
    kucoin_prices = fetch_prices('kucoin', EXCHANGES['kucoin']['inst'])
    mexc_prices = fetch_prices('mexc', EXCHANGES['mexc']['inst'])

    all_symbols = set(kucoin_prices.keys()) | set(mexc_prices.keys())

    for symbol in all_symbols:
        if symbol in BLACKLIST:
            continue

        kucoin = kucoin_prices.get(symbol)
        mexc = mexc_prices.get(symbol)

        if not kucoin or not mexc:
            continue

        price_kucoin = kucoin['price']
        price_mexc = mexc['price']
        spread_kucoin_to_mexc = (price_mexc - price_kucoin) / price_kucoin * 100
        spread_mexc_to_kucoin = (price_kucoin - price_mexc) / price_mexc * 100

        possible, network = check_transfer_possible('kucoin', 'mexc', symbol)

        if possible and spread_kucoin_to_mexc > SPREAD_THRESHOLD:
            opportunities.append({
                'pair': symbol,
                'spread': round(spread_kucoin_to_mexc, 2),
                'buy_exchange': 'kucoin',
                'sell_exchange': 'mexc',
                'network': network,
                'buy_price': price_kucoin,
                'sell_price': price_mexc,
                'volume': kucoin['volume']
            })
        elif possible and spread_mexc_to_kucoin > SPREAD_THRESHOLD:
            opportunities.append({
                'pair': symbol,
                'spread': round(spread_mexc_to_kucoin, 2),
                'buy_exchange': 'mexc',
                'sell_exchange': 'kucoin',
                'network': network,
                'buy_price': price_mexc,
                'sell_price': price_kucoin,
                'volume': mexc['volume']
            })

    opportunities.sort(key=lambda x: x['spread'], reverse=True)
    return opportunities

# ---------- Форматирование сообщения (новый дизайн) ----------
def format_message(opportunities):
    if not opportunities:
        return None

    lines = ["<b>ТОП-7 Возможностей (Вывод открыт:)</b>\n"]
    for opp in opportunities[:7]:
        pair = opp['pair']
        spread = opp['spread']
        buy_ex = opp['buy_exchange']
        sell_ex = opp['sell_exchange']
        network = opp.get('network', '—')
        buy_price = opp['buy_price']
        sell_price = opp['sell_price']
        volume = opp.get('volume', 0)

        buy_ex_display = "kuCoin" if buy_ex == 'kucoin' else "mexc"
        sell_ex_display = "kuCoin" if sell_ex == 'kucoin' else "mexc"

        # Форматирование цен: для мелких монет (<1) показываем до 6 знаков, для крупных – до 3
        buy_price_str = f"{buy_price:.6f}".rstrip('0').rstrip('.') if buy_price < 1 else f"{buy_price:.3f}".rstrip('0').rstrip('.')
        sell_price_str = f"{sell_price:.6f}".rstrip('0').rstrip('.') if sell_price < 1 else f"{sell_price:.3f}".rstrip('0').rstrip('.')

        volume_str = f"{volume:,.0f}"

        line = (
            f"{pair} (Vol: ${volume_str})\n"
            f"Купить: {buy_ex_display} ({buy_price_str})\n"
            f"Продать: {sell_ex_display} ({sell_price_str})\n\n"
            f"<b>Спред:</b> {spread}%\n"
            f"✅ Сеть: {network}\n"
        )
        lines.append(line)

    return "\n".join(lines)

# ---------- Обработчик команд ----------
def handle_update(update_json):
    try:
        logging.debug(f"Update: {update_json}")

        if "callback_query" in update_json:
            cb = update_json["callback_query"]
            chat_id = cb["message"]["chat"]["id"]
            data = cb["data"]
            requests.post(f"{TELEGRAM_API_URL}/answerCallbackQuery",
                          json={"callback_query_id": cb["id"]})
            if data == "check_payment":
                send_message(chat_id, "📸 Пожалуйста, отправьте скриншот подтверждения перевода.\nАдминистратор проверит и активирует доступ вручную.")
            return

        if "message" not in update_json:
            return

        msg = update_json["message"]
        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        username = msg["from"].get("username")
        text = msg.get("text", "")

        if text == "/start":
            cmd_start(chat_id, user_id, username)
        elif text.startswith("/grant") and user_id in ADMIN_IDS:
            cmd_grant(chat_id, text)
        elif text == "🚀 Найти арбитраж (сейчас)":
            cmd_manual_scan(chat_id, user_id)
        elif text == "💰 Купить подписку 2000₽/мес":
            cmd_buy(chat_id)
        elif text == "▶️ Авто-скан (1 мин)":
            cmd_auto_scan(chat_id, user_id, 1)
        elif text == "▶️ Авто-скан (5 мин)":
            cmd_auto_scan(chat_id, user_id, 5)
        elif text == "⏹ Стоп сканирование":
            cmd_stop_auto(chat_id, user_id)
        elif text == "📊 Статус":
            cmd_status(chat_id, user_id)
        elif msg.get('photo'):
            handle_photo(chat_id, user_id, username, msg)
        else:
            send_message_with_keyboard(chat_id, "Неизвестная команда. Используйте кнопки.", get_main_keyboard())

    except Exception as e:
        logging.error(f"Error in handle_update: {e}", exc_info=True)

# ---------- Реализация команд ----------
def cmd_start(chat_id, user_id, username):
    if is_subscribed(user_id):
        send_message_with_keyboard(
            chat_id,
            f"👋 Добро пожаловать, {username or 'пользователь'}!\nБот готов к работе.",
            get_main_keyboard()
        )
    else:
        send_message_with_keyboard(
            chat_id,
            "👋 Привет! Я бот для арбитража криптовалют.\nДля использования необходимо оформить подписку.",
            [["💰 Купить подписку 2000₽/мес"]]
        )

def cmd_grant(chat_id, text):
    parts = text.split()
    if len(parts) < 2:
        send_message(chat_id, "Использование: /grant <user_id> [days]")
        return
    try:
        target_id = int(parts[1])
        days = int(parts[2]) if len(parts) > 2 else 30
    except ValueError:
        send_message(chat_id, "Неверный формат.")
        return
    grant_access(target_id, None, days)
    send_message(chat_id, f"✅ Доступ пользователю {target_id} выдан на {days} дней.")
    try:
        send_message(target_id, f"🎉 Вам выдана подписка на {days} дней!")
    except:
        pass

def cmd_manual_scan(chat_id, user_id):
    if not is_subscribed(user_id):
        send_message(chat_id, "❌ У вас нет активной подписки.\n💰 Оплатите через меню.")
        return
    send_message(chat_id, "⏳ Ищу только открытые сети...")
    opportunities = scan_market()
    text = format_message(opportunities)
    if not text:
        send_message(chat_id, "🙁 Вилок с открытыми сетями сейчас нет.")
    else:
        send_message(chat_id, text)

def cmd_buy(chat_id):
    markup = get_payment_keyboard()
    send_message(
        chat_id,
        "🔥 <b>Оплата доступа к боту</b>\n\n"
        "💰 Стоимость: <b>2000₽/месяц</b>\n"
        "💎 Способ оплаты: СБП (мгновенно, без комиссии)\n\n"
        "👉 Нажмите кнопку ниже, чтобы перейти к оплате.\n"
        "После перевода нажмите «Я оплатил» и пришлите скриншот.",
        reply_markup=markup
    )

def cmd_auto_scan(chat_id, user_id, minutes):
    if not is_subscribed(user_id):
        send_message(chat_id, "❌ У вас нет активной подписки.")
        return
    enable_auto_scan(user_id, minutes)
    send_message(chat_id, f"✅ Авто-скан включён (интервал {minutes} мин).")

def cmd_stop_auto(chat_id, user_id):
    disable_auto_scan(user_id)
    send_message(chat_id, "🛑 Авто-скан отключён.")

def cmd_status(chat_id, user_id):
    send_message(chat_id, "📊 Статус: работает")

def handle_photo(chat_id, user_id, username, msg):
    if not ADMIN_IDS:
        send_message(chat_id, "❌ Ошибка: не настроен администратор.")
        return
    file_id = msg['photo'][-1]['file_id']
    caption = f"💰 Платёж от @{username} (ID: {user_id})\nПроверьте и выдайте доступ."
    url = f"{TELEGRAM_API_URL}/sendPhoto"
    payload = {
        "chat_id": ADMIN_IDS[0],
        "photo": file_id,
        "caption": caption
    }
    try:
        requests.post(url, json=payload, timeout=10)
        send_message(chat_id, "✅ Скриншот отправлен администратору. Ожидайте подтверждения.")
    except Exception as e:
        logging.error(f"Failed to forward photo: {e}")
        send_message(chat_id, "❌ Ошибка при отправке скриншота.")