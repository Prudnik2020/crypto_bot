import sys
import asyncio
import os
import sqlite3
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import ccxt.async_support as ccxt
import pandas as pd
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram import F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# --- ЧТЕНИЕ НАСТРОЕК ИЗ .env ---
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
if ADMIN_IDS_STR:
    CHAT_ID = int(ADMIN_IDS_STR.split(',')[0].strip())
    ADMIN_ID = int(ADMIN_IDS_STR.split(',')[0].strip())  # для платежей
else:
    CHAT_ID = None
    ADMIN_ID = None
    logging.error("ADMIN_IDS не задан в .env")

MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET = os.getenv("MEXC_SECRET")

# --- НАСТРОЙКИ СКАНЕРА ---
SPREAD_THRESHOLD = 0.1   # минимальный спред 0.1%
MIN_VOLUME = 1000        # минимальный объём 1000$
BLACKLIST = ['USDC/USDT', 'USDD/USDT', 'BUSD/USDT', 'DAI/USDT', 'TUSD/USDT', 'FDUSD/USDT', 'X/USDT']

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

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

currency_data = {}
auto_scan_task = None
network_update_task = None

# --- БАЗА ДАННЫХ ПОДПИСОК ---
conn = sqlite3.connect('subscriptions.db')
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
             (user_id INTEGER PRIMARY KEY,
              username TEXT,
              expires DATE)''')
conn.commit()

# --- ТАБЛИЦА ДЛЯ НАСТРОЕК АВТО-СКАНА ---
c.execute('''CREATE TABLE IF NOT EXISTS auto_scan
             (user_id INTEGER PRIMARY KEY,
              interval INTEGER,  -- 1 или 5 минут
              last_sent TIMESTAMP)''')
conn.commit()

def is_subscribed(user_id):
    """Проверяет, активна ли подписка у пользователя."""
    c.execute("SELECT expires FROM subscriptions WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if row:
        expires = datetime.strptime(row[0], '%Y-%m-%d').date()
        if expires >= datetime.now().date():
            return True
    return False

def grant_access(user_id, username, days=30):
    """Выдаёт доступ на указанное количество дней (по умолчанию 30)."""
    expires = (datetime.now() + timedelta(days=days)).date()
    c.execute("REPLACE INTO subscriptions (user_id, username, expires) VALUES (?,?,?)",
              (user_id, username, expires.isoformat()))
    conn.commit()
    return expires

def enable_auto_scan(user_id, interval):
    """Включает авто-скан для пользователя с заданным интервалом (минуты)."""
    now = datetime.now().isoformat()
    c.execute("REPLACE INTO auto_scan (user_id, interval, last_sent) VALUES (?,?,?)",
              (user_id, interval, now))
    conn.commit()

def disable_auto_scan(user_id):
    """Отключает авто-скан для пользователя."""
    c.execute("DELETE FROM auto_scan WHERE user_id=?", (user_id,))
    conn.commit()

def get_auto_scan_users():
    """Возвращает список пользователей с включённым авто-сканом."""
    c.execute("SELECT user_id, interval, last_sent FROM auto_scan")
    rows = c.fetchall()
    return rows  # каждая строка: (user_id, interval, last_sent)

def update_last_sent(user_id, last_sent):
    """Обновляет время последней отправки для пользователя."""
    c.execute("UPDATE auto_scan SET last_sent=? WHERE user_id=?", (last_sent.isoformat(), user_id))
    conn.commit()

# ========== ФУНКЦИИ ==========

def get_link(exchange_name, symbol):
    base = symbol.split('/')[0]
    return EXCHANGES[exchange_name]['url'].format(base)

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

async def fetch_network_data(exchange_name, exchange):
    data = {}
    try:
        await exchange.load_markets()
        logging.info(f"{exchange_name}: загружаем currencies...")
        currencies = await exchange.fetch_currencies()
        if not currencies:
            logging.warning(f"{exchange_name}: currencies пуст!")
            return exchange_name, data
        logging.info(f"{exchange_name}: получено {len(currencies)} валют. Примеры: {list(currencies.keys())[:5]}")

        for code, cur_data in currencies.items():
            active_networks = []
            # Стандартный способ через networks
            nets = cur_data.get('networks', {})
            if nets:
                for net_id, net_info in nets.items():
                    if not net_info: continue
                    info = net_info.get('info', {})
                    deposit = info.get('depositEnable', net_info.get('deposit'))
                    withdraw = info.get('withdrawEnable', net_info.get('withdraw'))
                    d_status = str(deposit).lower() in ['true', '1']
                    w_status = str(withdraw).lower() in ['true', '1']
                    if d_status or w_status:
                        net_name = info.get('chain', net_info.get('network', net_id))
                        active_networks.append({'name': normalize_network(net_name), 'deposit': d_status, 'withdraw': w_status})

            # Запасной способ для MEXC (если networks пуст)
            if not active_networks and 'info' in cur_data:
                info = cur_data['info']
                raw_chains = info.get('chains', info.get('coins', []))
                if not isinstance(raw_chains, list):
                    raw_chains = []
                for chain in raw_chains:
                    if not chain: continue
                    net_name = chain.get('showName', chain.get('chain', chain.get('name')))
                    d_status = str(chain.get('depositStatus', chain.get('depositEnable', 0))) in ['true', '1', 'opened']
                    w_status = str(chain.get('withdrawStatus', chain.get('withdrawEnable', 0))) in ['true', '1', 'opened']
                    if chain.get('withdrawEnable') == 1: w_status = True
                    if chain.get('depositEnable') == 1: d_status = True
                    if net_name and (d_status or w_status):
                        active_networks.append({'name': normalize_network(net_name), 'deposit': d_status, 'withdraw': w_status})

            if active_networks:
                data[code] = active_networks
                logging.debug(f"{exchange_name}: {code} -> {active_networks}")
    except Exception as e:
        logging.error(f"Ошибка парсинга сетей {exchange_name}: {e}")
    return exchange_name, data

async def update_network_data():
    global currency_data
    logging.info("🔄 Глубокое сканирование сетей (MEXC/KuCoin)...")
    tasks = [fetch_network_data(name, data['inst']) for name, data in EXCHANGES.items()]
    results = await asyncio.gather(*tasks)
    for name, data in results:
        currency_data[name] = data
    # Выведем краткую статистику
    for ex in currency_data:
        coins_with_networks = len(currency_data[ex])
        logging.info(f"{ex}: монет с данными о сетях: {coins_with_networks}")
    logging.info("✅ Данные сетей обновлены.")

def check_transfer_possible(buy_ex, sell_ex, symbol):
    base_coin = symbol.split('/')[0]
    if buy_ex not in currency_data or sell_ex not in currency_data:
        return False, "❌ Нет данных по бирже"
    buy_nets = currency_data[buy_ex].get(base_coin, [])
    sell_nets = currency_data[sell_ex].get(base_coin, [])
    logging.debug(f"check_transfer_possible: {symbol} buy_nets={buy_nets}, sell_nets={sell_nets}")
    if not buy_nets or not sell_nets:
        return False, "❌ Нет сетей у монеты"
    sell_map = {n['name']: n for n in sell_nets}
    for b in buy_nets:
        if b['name'] in sell_map:
            s = sell_map[b['name']]
            if b['withdraw'] and s['deposit']:
                return True, f"✅ Сеть: {b['name']}"
    return False, "❌ Нет общих сетей"

async def fetch_prices(exchange_name, exchange_data):
    exchange = exchange_data['inst']
    try:
        tickers = await exchange.fetch_tickers()
        data = []
        for symbol, ticker in tickers.items():
            if '/USDT' not in symbol: continue
            if any(x in symbol for x in BLACKLIST): continue
            if not ticker.get('last') or not ticker.get('quoteVolume'): continue
            volume = ticker.get('quoteVolume', 0)
            if volume < MIN_VOLUME: continue
            data.append({'exchange': exchange_name, 'symbol': symbol, 'price': ticker['last'], 'volume': volume})
        logging.info(f"{exchange_name}: получено {len(data)} тикеров (после фильтров)")
        return data
    except Exception as e:
        logging.error(f"Ошибка fetch_prices {exchange_name}: {e}")
        return []

async def scan_market():
    all_data = []
    tasks = [fetch_prices(name, data) for name, data in EXCHANGES.items()]
    results = await asyncio.gather(*tasks)
    for res in results:
        all_data.extend(res)
    logging.info(f"Всего тикеров после фильтров: {len(all_data)}")
    if not all_data:
        return []
    df = pd.DataFrame(all_data)
    opportunities = []
    grouped = df.groupby('symbol')
    logging.info(f"Найдено уникальных символов: {len(grouped)}")
    for symbol, group in grouped:
        if len(group) < 2:
            continue
        min_row = group.loc[group['price'].idxmin()]
        max_row = group.loc[group['price'].idxmax()]
        if min_row['exchange'] == max_row['exchange']:
            continue
        spread = ((max_row['price'] - min_row['price']) / min_row['price']) * 100
        if spread > 50:  # отсекаем явные глюки
            continue
        if spread >= SPREAD_THRESHOLD:
            buy_ex = min_row['exchange']
            sell_ex = max_row['exchange']
            can_transfer, comment = check_transfer_possible(buy_ex, sell_ex, symbol)
            if not can_transfer:
                continue
            opportunities.append({
                'symbol': symbol, 'buy_ex': buy_ex, 'buy_price': min_row['price'],
                'sell_ex': sell_ex, 'sell_price': max_row['price'],
                'spread': round(spread, 2), 'volume': int(min_row['volume']),
                'transfer_comment': comment
            })
    logging.info(f"Найдено возможностей после проверки сетей: {len(opportunities)}")
    opportunities.sort(key=lambda x: x['spread'], reverse=True)
    return opportunities

def format_message(opps):
    if not opps:
        return None
    text = "<b>🔥 ТОП-7 Возможностей (Вывод открыт:)</b>\n\n"
    for o in opps[:7]:
        link_buy = get_link(o['buy_ex'], o['symbol'])
        link_sell = get_link(o['sell_ex'], o['symbol'])
        text += (
            f"💰 <b>{o['symbol']}</b> (Vol: ${o['volume']:,})\n"
            f"🔹 Купить: <a href='{link_buy}'>{o['buy_ex']}</a> ({o['buy_price']})\n"
            f"🔸 Продать: <a href='{link_sell}'>{o['sell_ex']}</a> ({o['sell_price']})\n"
            f"📈 <b>Спред: {o['spread']}%</b>\n"
            f"🔄 {o['transfer_comment']}\n"
            f"───────────────\n"
        )
    return text

# ========== ПЛАТЁЖНЫЕ ХЕНДЛЕРЫ ==========
@dp.message(F.text == "💰 Купить подписку 2000₽/мес")
async def cmd_buy(message: types.Message):
    # ЗАМЕНИТЕ на свою ссылку СБП
    sbp_link = "https://www.sberbank.ru/ru/choise_bank?requisiteNumber=79093545631&bankCode=100000000111"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить через СБП", url=sbp_link)],
        [InlineKeyboardButton(text="✅ Я оплатил (проверить)", callback_data="check_payment")]
    ])

    await message.answer(
        "🔥 <b>Оплата доступа к боту</b>\n\n"
        "💰 Стоимость: <b>2000₽/месяц</b>\n"
        "💎 Способ оплаты: СБП (мгновенно, без комиссии)\n\n"
        "👉 Нажмите кнопку ниже, чтобы перейти к оплате.\n"
        "После перевода нажмите «Я оплатил» и пришлите скриншот.",
        reply_markup=kb,
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "check_payment")
async def process_payment_check(callback: types.CallbackQuery):
    await callback.message.answer(
        "📸 Пожалуйста, отправьте скриншот подтверждения перевода.\n"
        "Администратор проверит и активирует доступ вручную (обычно в течение 5–10 минут)."
    )
    await callback.answer()

@dp.message(F.photo)
async def handle_payment_screenshot(message: types.Message):
    if not ADMIN_ID:
        await message.answer("❌ Ошибка: не настроен администратор.")
        return

    caption = f"💰 Платёж от @{message.from_user.username} (ID: {message.from_user.id})\nПроверьте и выдайте доступ."
    await bot.send_photo(
        chat_id=ADMIN_ID,
        photo=message.photo[-1].file_id,
        caption=caption
    )

    await message.answer("✅ Скриншот отправлен администратору. Ожидайте подтверждения.")

# ========== КОМАНДА АДМИНИСТРАТОРА ==========
@dp.message(Command("grant"))
async def cmd_grant(message: types.Message):
    # Проверяем, что команда от администратора
    if message.from_user.id != ADMIN_ID:
        return

    args = message.text.split()
    
    # Если есть аргументы, пробуем интерпретировать как ID пользователя
    if len(args) >= 2:
        try:
            user_id = int(args[1])
            days = 30
            if len(args) >= 3:
                days = int(args[2])
            
            expires = grant_access(user_id, f"id{user_id}", days)
            await message.answer(f"✅ Подписка выдана пользователю {user_id} на {days} дней (до {expires}).")
            
            try:
                await bot.send_message(
                    user_id,
                    f"🎉 Вам выдана подписка на {days} дней! Теперь вам доступен поиск арбитража."
                )
            except Exception as e:
                logging.error(f"Не удалось уведомить пользователя {user_id}: {e}")
            return
        except ValueError:
            await message.answer("❌ Неверный формат ID. Используйте: /grant user_id [дни]")
            return

    if not message.reply_to_message:
        await message.answer("❌ Ответьте на сообщение пользователя или укажите ID: /grant user_id [дни]")
        return

    user = message.reply_to_message.from_user
    days = 30
    if len(args) > 1:
        try:
            days = int(args[1])
        except ValueError:
            await message.answer("❌ Количество дней должно быть числом. Использование: /grant [дни]")
            return

    expires = grant_access(user.id, user.username or f"id{user.id}", days)
    await message.answer(f"✅ Подписка выдана пользователю @{user.username} на {days} дней (до {expires}).")

    try:
        await bot.send_message(
            user.id,
            f"🎉 Вам выдана подписка на {days} дней! Теперь вам доступен поиск арбитража."
        )
    except Exception as e:
        logging.error(f"Не удалось уведомить пользователя {user.id}: {e}")

# ========== ОСНОВНЫЕ ХЕНДЛЕРЫ ==========
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await update_network_data()
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [types.KeyboardButton(text="🚀 Найти арбитраж (сейчас)")],
        [types.KeyboardButton(text="💰 Купить подписку 2000₽/мес")],
        [types.KeyboardButton(text="▶️ Авто-скан (1 мин)"), types.KeyboardButton(text="▶️ Авто-скан (5 мин)")],
        [types.KeyboardButton(text="⏹ Стоп сканирование")],
        [types.KeyboardButton(text="📊 Статус")]
    ])
    await message.answer("👋 Бот готов! (режим отладки)", reply_markup=kb)

@dp.message(lambda msg: msg.text == "📊 Статус")
async def cmd_status(message: types.Message):
    status = "Работает" if auto_scan_task and not auto_scan_task.done() else "Остановлен"
    await message.answer(f"📊 Статус: {status}")

@dp.message(lambda msg: msg.text == "🚀 Найти арбитраж (сейчас)")
async def cmd_manual_scan(message: types.Message):
    if not is_subscribed(message.from_user.id):
        await message.answer(
            "❌ У вас нет активной подписки.\n"
            "💰 Чтобы получить доступ к сигналам, оплатите подписку через меню."
        )
        return

    status_msg = await message.answer("⏳ Ищу только открытые сети...")
    opps = await scan_market()
    await status_msg.delete()
    text = format_message(opps)
    if not text:
        await message.answer("🙁 Вилок с открытыми сетями сейчас нет.")
    else:
        await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)

@dp.message(lambda msg: "▶️ Авто-скан" in msg.text)
async def cmd_start_auto(message: types.Message):
    # Проверяем подписку
    if not is_subscribed(message.from_user.id):
        await message.answer(
            "❌ У вас нет активной подписки.\n"
            "💰 Чтобы использовать авто-скан, оплатите подписку через меню."
        )
        return

    if "1 мин" in message.text:
        interval = 1
    else:
        interval = 5

    enable_auto_scan(message.from_user.id, interval)
    await message.answer(f"✅ Авто-скан включён (интервал {interval} мин). Вы будете получать сигналы.")

    # Запускаем общую задачу, если ещё не запущена
    global auto_scan_task
    if not auto_scan_task or auto_scan_task.done():
        auto_scan_task = asyncio.create_task(auto_scan_worker())

@dp.message(lambda msg: msg.text == "⏹ Стоп сканирование")
async def cmd_stop_auto(message: types.Message):
    disable_auto_scan(message.from_user.id)
    await message.answer("🛑 Авто-скан отключён. Вы больше не будете получать сигналы.")

    # Если больше ни у кого нет автоскана, можно остановить задачу (опционально)
    if not get_auto_scan_users():
        global auto_scan_task
        if auto_scan_task and not auto_scan_task.done():
            auto_scan_task.cancel()
            auto_scan_task = None

# ========== ВОРКЕРЫ ==========
async def network_update_worker():
    while True:
        await asyncio.sleep(30 * 60)
        logging.info("⏳ Плановое обновление данных сетей...")
        await update_network_data()

# ========== АВТО-СКАН (индивидуальный) ==========
async def auto_scan_worker():
    while True:
        try:
            # Ждём 60 секунд перед каждой проверкой
            await asyncio.sleep(60)
            now = datetime.now()
            users = get_auto_scan_users()  # список (user_id, interval, last_sent)
            if not users:
                continue

            # Сканируем рынок один раз для всех
            opps = await scan_market()
            text = format_message(opps)
            if not text:
                logging.info("Вилок с открытыми сетями не найдено.")
                continue

            for user_id, interval, last_sent_str in users:
                last_sent = datetime.fromisoformat(last_sent_str)
                # Проверяем, прошло ли достаточно времени
                if now - last_sent >= timedelta(minutes=interval):
                    try:
                        await bot.send_message(user_id, text, parse_mode="HTML", disable_web_page_preview=True)
                        update_last_sent(user_id, now)
                    except Exception as e:
                        logging.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.error(f"Ошибка в авто-воркере: {e}")

# ========== MAIN ==========
async def main():
    global network_update_task
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.error("Не задан TELEGRAM_TOKEN или ADMIN_IDS. Бот остановлен.")
        return
    network_update_task = asyncio.create_task(network_update_worker())
    await update_network_data()
    logging.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Остановлено")