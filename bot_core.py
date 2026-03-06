import os
import sqlite3
import logging
import json
import requests
from datetime import datetime, timedelta
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

KUCOIN_API_KEY = os.getenv("KUCOIN_API_KEY")
KUCOIN_SECRET = os.getenv("KUCOIN_SECRET")
KUCOIN_PASSPHRASE = os.getenv("KUCOIN_PASSPHRASE")

# ---------- Настройки сканера ----------
SPREAD_THRESHOLD = 1.0
MAX_SPREAD = 15.0  # Изменено на 15%
MIN_VOLUME = 1000
BLACKLIST = ['USDC/USDT', 'USDD/USDT', 'BUSD/USDT', 'DAI/USDT', 'TUSD/USDT', 'FDUSD/USDT', 'X/USDT']

# ---------- Биржи и шаблоны ссылок ----------
EXCHANGES = {
    'kucoin': {
        'inst': ccxt.kucoin({
            'apiKey': KUCOIN_API_KEY,
            'secret': KUCOIN_SECRET,
            'password': KUCOIN_PASSPHRASE,
            'enableRateLimit': True
        }),
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

# ---------- НОВЫЕ ФУНКЦИИ ДЛЯ АВТОСКАНА ----------
def update_last_sent(user_id, last_sent):
    """Обновляет время последней отправки для пользователя"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE auto_scan SET last_sent=? WHERE user_id=?", (last_sent.isoformat(), user_id))
    conn.commit()
    conn.close()

def run_autoscan():
    """
    Вызывается по крону раз в минуту. Проверяет всех пользователей с автосканом
    и отправляет результаты, если наступило время.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, interval, last_sent FROM auto_scan")
    rows = c.fetchall()
    conn.close()

    now = datetime.now()
    for user_id, interval, last_sent_str in rows:
        if last_sent_str:
            last_sent = datetime.fromisoformat(last_sent_str)
        else:
            last_sent = None

        # Проверяем, пора ли отправлять
        if last_sent is None or (now - last_sent).total_seconds() >= interval * 60:
            opportunities = scan_market()
            text = format_message(opportunities)
            if text:
                send_message(user_id, text, disable_web_page_preview=True)
            update_last_sent(user_id, now)
# ----------------------------------------------------

# ---------- Отправка сообщений ----------
def send_message(chat_id, text, reply_markup=None, parse_mode="HTML", disable_web_page_preview=False):
    url = f"{TELEGRAM_API_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview
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
    return {
        "inline_keyboard": [
            [{"text": "✅ Я оплатил (проверить)", "callback_data": "check_payment"}]
        ]
    }

# ---------- Нормализация названий сетей ----------
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

# ---------- Получение активных сетей с KuCoin (публичный эндпоинт) ----------
def fetch_kucoin_active_networks():
    """Возвращает словарь {валюта: [список активных сетей]} для KuCoin"""
    url = "https://api.kucoin.com/api/v3/currencies"
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
        if data.get('code') == '200000':
            result = {}
            for item in data.get('data', []):
                currency = item.get('currency')
                if not currency:
                    continue
                active_nets = []
                chains = item.get('chains', [])
                if isinstance(chains, list):
                    for chain in chains:
                        if chain.get('isDepositEnabled') and chain.get('isWithdrawEnabled'):
                            net_name = normalize_network(chain.get('chainName', ''))
                            if net_name:
                                active_nets.append(net_name)
                if active_nets:
                    result[currency] = active_nets
            logging.info(f"KuCoin: загружено {len(result)} валют с активными сетями")
            return result
        else:
            logging.error(f"KuCoin API error: {data}")
    except Exception as e:
        logging.error(f"Ошибка получения сетей KuCoin: {e}")
    return {}

# ---------- Получение активных сетей с MEXC (через ccxt) ----------
def fetch_mexc_active_networks():
    """Возвращает словарь {валюта: [список активных сетей]} для MEXC"""
    try:
        mexc = EXCHANGES['mexc']['inst']
        currencies = mexc.fetch_currencies()
        if not isinstance(currencies, dict):
            logging.error(f"MEXC fetch_currencies вернул не словарь: {type(currencies)}")
            return {}
        result = {}
        for code, data in currencies.items():
            if not isinstance(data, dict):
                continue
            active_nets = []
            networks = data.get('networks')
            if isinstance(networks, dict):
                for net_name, net_data in networks.items():
                    if not isinstance(net_data, dict):
                        continue
                    deposit = net_data.get('deposit')
                    withdraw = net_data.get('withdraw')
                    # deposit и withdraw могут быть словарями или булевыми значениями
                    # В ccxt для MEXC они обычно словари с полем 'enabled'
                    deposit_ok = False
                    withdraw_ok = False
                    if isinstance(deposit, dict):
                        deposit_ok = deposit.get('enabled', False)
                    elif isinstance(deposit, bool):
                        deposit_ok = deposit
                    if isinstance(withdraw, dict):
                        withdraw_ok = withdraw.get('enabled', False)
                    elif isinstance(withdraw, bool):
                        withdraw_ok = withdraw
                    if deposit_ok and withdraw_ok:
                        norm_net = normalize_network(net_name)
                        if norm_net:
                            active_nets.append(norm_net)
            if active_nets:
                result[code] = active_nets
        logging.info(f"MEXC: загружено {len(result)} валют с активными сетями")
        return result
    except Exception as e:
        logging.error(f"Ошибка получения сетей MEXC: {e}")
        return {}

# ---------- Глобальный словарь активных сетей ----------
active_networks = {
    'kucoin': {},
    'mexc': {}
}

def update_active_networks():
    """Обновляет данные об активных сетях для обеих бирж"""
    global active_networks
    active_networks['kucoin'] = fetch_kucoin_active_networks()
    active_networks['mexc'] = fetch_mexc_active_networks()
    logging.info("Данные об активных сетях обновлены")

# Вызываем при старте
update_active_networks()

# ---------- Проверка возможности перевода с учётом реально работающих сетей ----------
def check_transfer_possible(buy_ex, sell_ex, symbol):
    base = symbol.split('/')[0]
    buy_nets = active_networks.get(buy_ex, {}).get(base, [])
    sell_nets = active_networks.get(sell_ex, {}).get(base, [])
    common = set(buy_nets) & set(sell_nets)
    if common:
        return True, list(common)[0]
    else:
        # Если для какой-то из бирж данные не загрузились (пустой словарь), считаем перевод возможным с пометкой "?"
        if not active_networks.get(buy_ex) or not active_networks.get(sell_ex):
            return True, "?"
        # Если данные есть, но нет общей сети – перевод невозможен
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

        if possible and SPREAD_THRESHOLD < spread_kucoin_to_mexc <= MAX_SPREAD:
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
        elif possible and SPREAD_THRESHOLD < spread_mexc_to_kucoin <= MAX_SPREAD:
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

# ---------- Форматирование сообщения (точь-в-точь как в старом коде) ----------
def format_message(opportunities):
    if not opportunities:
        return None

    lines = ["🔥 <b>ТОП-7 Возможностей (Вывод открыт:)</b>\n"]
    for opp in opportunities[:7]:
        pair = opp['pair']
        spread = opp['spread']
        buy_ex = opp['buy_exchange']
        sell_ex = opp['sell_exchange']
        network = opp.get('network', '—')
        buy_price = opp['buy_price']
        sell_price = opp['sell_price']
        volume = opp.get('volume', 0)

        buy_link = EXCHANGES[buy_ex]['url'].format(pair.split('/')[0])
        sell_link = EXCHANGES[sell_ex]['url'].format(pair.split('/')[0])

        buy_name = "kucoin" if buy_ex == 'kucoin' else "mexc"
        sell_name = "kucoin" if sell_ex == 'kucoin' else "mexc"

        buy_price_str = f"{buy_price:.6f}".rstrip('0').rstrip('.') if buy_price < 1 else f"{buy_price:.3f}".rstrip('0').rstrip('.')
        sell_price_str = f"{sell_price:.6f}".rstrip('0').rstrip('.') if sell_price < 1 else f"{sell_price:.3f}".rstrip('0').rstrip('.')
        volume_str = f"{volume:,.0f}"

        line = (
            f"💰 <b>{pair}</b> (Vol: ${volume_str})\n"
            f"🔹 Купить: <a href='{buy_link}'>{buy_name}</a> ({buy_price_str})\n"
            f"🔸 Продать: <a href='{sell_link}'>{sell_name}</a> ({sell_price_str})\n"
            f"📈 <b>Спред: {spread}%</b>\n"
            f"🔄 ✅ Сеть: {network}\n"
            f"───────────────\n"
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
        send_message(chat_id, text, disable_web_page_preview=True)

def cmd_buy(chat_id):
    markup = get_payment_keyboard()
    send_message(
        chat_id,
        "🔥 <b>Оплата доступа к боту</b>\n\n"
        "💰 Стоимость: <b>2000₽/месяц</b>\n"
        "💳 Номер карты Сбербанк (МИР):\n"
        "<code>2202 2008 6542 7262</code>\n\n"
        "👉 Переведите указанную сумму на эту карту.\n"
        "После перевода нажмите кнопку «✅ Я оплатил» и пришлите скриншот подтверждения.\n\n"
        "Администратор проверит и активирует доступ вручную.",
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