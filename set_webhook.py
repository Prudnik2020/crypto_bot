import sys
import asyncio
from aiogram import Bot

# === ЭТА СТРОКА РЕШАЕТ ПРОБЛЕМУ ===
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
# ===================================

TOKEN = "8291526907:AAGT_l9gE4kMjZcDx9WxXtVISRDihe8DfEY"  # ВСТАВЬТЕ СЮДА НОВЫЙ ТОКЕН!
WEBHOOK_URL = "https://Prudnik.pythonanywhere.com/webhook"

async def main():
    bot = Bot(token=TOKEN)
    await bot.set_webhook(url=WEBHOOK_URL)
    print("✅ Webhook успешно установлен!")

if __name__ == "__main__":
    asyncio.run(main())