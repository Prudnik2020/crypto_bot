# bot_core.py
import sys
import asyncio
import os
import sqlite3
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Загружаем .env (для локальной отладки)
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
    ADMIN_ID = int(ADMIN_IDS_STR.split(',')[0].strip())
else:
    CHAT_ID = None
    ADMIN_ID = None
    logging.error("ADMIN_IDS не задан в .env")

MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET = os.getenv("MEXC_SECRET")

# --- НАСТРОЙКИ СКАНЕРА ---
SPREAD_THRESHOLD = 0.1
MIN_VOLUME = 1000
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
conn = sqlite3.connect('subscriptions.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
             (user_id INTEGER PRIMARY KEY,
              username TEXT,
              expires DATE)''')
conn.commit()

c.execute('''CREATE TABLE IF NOT EXISTS auto_scan
             (user_id INTEGER PRIMARY KEY,
              interval INTEGER,
              last_sent TIMESTAMP)''')
conn.commit()

def is_subscribed(user_id):
    c.execute("SELECT expires FROM subscriptions WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if row:
        expires = datetime.strptime(row[0], '%Y-%m-%d').date()
        if expires >= datetime.now().date():
            return True
    return False

def grant_access(user_id, username, days=30):
    expires = (datetime.now() + timedelta(days=days)).date()
    c.execute("REPLACE INTO subscriptions (user_id, username, expires) VALUES (?,?,?)",
              (user_id, username, expires.isoformat()))
    conn.commit()
    return expires

def enable_auto_scan(user_id, interval):
    now = datetime.now().isoformat()
    c.execute("REPLACE INTO auto_scan (user_id, interval, last_sent) VALUES (?,?,?)",
              (user_id, interval, now))
    conn.commit()

def disable_auto_scan(user_id):
    c.execute("DELETE FROM auto_scan WHERE user_id=?", (user_id,))
    conn.commit()

def get_auto_scan_users():
    c.execute("SELECT user_id, interval, last_sent FROM auto_scan")
    return c.fetchall()

def update_last_sent(user_id, last_sent):
    c.execute("UPDATE auto_scan SET last_sent=? WHERE user_id=?", (last_sent.isoformat(), user_id))
    conn.commit()

# ========== ФУНКЦИИ СКАНЕРА (без изменений) ==========
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
    # ... (полностью скопируйте из вашего кода, без изменений)
    # Я оставляю многоточие для краткости, но вы должны вставить полную функцию.
    pass

async def update_network_data():
    # ... (ваша функция)
    pass

def check_transfer_possible(buy_ex, sell_ex, symbol):
    # ... (ваша функция)
    pass

async def fetch_prices(exchange_name, exchange_data):
    # ... (ваша функция)
    pass

async def scan_market():
    # ... (ваша функция)
    pass

def format_message(opps):
    # ... (ваша функция)
    pass

# ========== ХЕНДЛЕРЫ ==========
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
    await message.answer("👋 Бот готов!", reply_markup=kb)

@dp.message(lambda msg: msg.text == "📊 Статус")
async def cmd_status(message: types.Message):
    status = "Работает" if auto_scan_task and not auto_scan_task.done() else "Остановлен"
    await message.answer(f"📊 Статус: {status}")

@dp.message(lambda msg: msg.text == "🚀 Найти арбитраж (сейчас)")
async def cmd_manual_scan(message: types.Message):
    if not is_subscribed(message.from_user.id):
        await message.answer("❌ У вас нет активной подписки.\n💰 Оплатите через меню.")
        return
    status_msg = await message.answer("⏳ Ищу только открытые сети...")
    opps = await scan_market()
    await status_msg.delete()
    text = format_message(opps)
    if not text:
        await message.answer("🙁 Вилок с открытыми сетями сейчас нет.")
    else:
        await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)

@dp.message(F.text == "💰 Купить подписку 2000₽/мес")
async def cmd_buy(message: types.Message):
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
        reply_markup=kb, parse_mode="HTML"
    )

@dp.callback_query(F.data == "check_payment")
async def process_payment_check(callback: types.CallbackQuery):
    await callback.message.answer(
        "📸 Пожалуйста, отправьте скриншот подтверждения перевода.\n"
        "Администратор проверит и активирует доступ вручную."
    )
    await callback.answer()

@dp.message(F.photo)
async def handle_payment_screenshot(message: types.Message):
    if not ADMIN_ID:
        await message.answer("❌ Ошибка: не настроен администратор.")
        return
    caption = f"💰 Платёж от @{message.from_user.username} (ID: {message.from_user.id})\nПроверьте и выдайте доступ."
    await bot.send_photo(chat_id=ADMIN_ID, photo=message.photo[-1].file_id, caption=caption)
    await message.answer("✅ Скриншот отправлен администратору. Ожидайте подтверждения.")

@dp.message(Command("grant"))
async def cmd_grant(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) >= 2:
        try:
            user_id = int(args[1])
            days = 30
            if len(args) >= 3:
                days = int(args[2])
            expires = grant_access(user_id, f"id{user_id}", days)
            await message.answer(f"✅ Подписка выдана {user_id} на {days} дней (до {expires}).")
            try:
                await bot.send_message(user_id, f"🎉 Вам выдана подписка на {days} дней!")
            except:
                pass
            return
        except ValueError:
            await message.answer("❌ Неверный формат ID.")
            return
    if not message.reply_to_message:
        await message.answer("❌ Ответьте на сообщение пользователя или укажите ID.")
        return
    user = message.reply_to_message.from_user
    days = 30
    if len(args) > 1:
        try:
            days = int(args[1])
        except ValueError:
            await message.answer("❌ Количество дней должно быть числом.")
            return
    expires = grant_access(user.id, user.username or f"id{user.id}", days)
    await message.answer(f"✅ Подписка выдана @{user.username} на {days} дней (до {expires}).")
    try:
        await bot.send_message(user.id, f"🎉 Вам выдана подписка на {days} дней!")
    except:
        pass

@dp.message(lambda msg: "▶️ Авто-скан" in msg.text)
async def cmd_start_auto(message: types.Message):
    if not is_subscribed(message.from_user.id):
        await message.answer("❌ У вас нет активной подписки.")
        return
    interval = 1 if "1 мин" in message.text else 5
    enable_auto_scan(message.from_user.id, interval)
    await message.answer(f"✅ Авто-скан включён (интервал {interval} мин).")

@dp.message(lambda msg: msg.text == "⏹ Стоп сканирование")
async def cmd_stop_auto(message: types.Message):
    disable_auto_scan(message.from_user.id)
    await message.answer("🛑 Авто-скан отключён.")

# ========== ВОРКЕРЫ (теперь они не нужны для вебхука, но можно оставить) ==========
async def network_update_worker():
    while True:
        await asyncio.sleep(30 * 60)
        logging.info("⏳ Плановое обновление данных сетей...")
        await update_network_data()

async def auto_scan_worker():
    # Для вебхука этот воркер тоже должен работать, поэтому оставляем.
    # Он будет выполняться в фоне, если мы его запустим. Но для простоты можно запустить отдельно.
    pass

# Функция для обработки входящего обновления (вызывается из Flask)
async def handle_webhook(update_json):
    update = types.Update(**update_json)
    await dp.feed_update(bot, update)

# Функция для запуска фоновых задач (будет вызвана из Flask при старте)
async def start_background_tasks():
    global network_update_task, auto_scan_task
    network_update_task = asyncio.create_task(network_update_worker())
    # auto_scan_task = asyncio.create_task(auto_scan_worker()) # мы его переделаем позже