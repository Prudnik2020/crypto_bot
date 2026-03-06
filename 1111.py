import sys
import asyncio
from aiogram import Bot

# === ЭТА СТРОКА РЕШАЕТ ПРОБЛЕМУ НА WINDOWS ===
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
# ===============================================

TOKEN = "8291526907:AAGT_l9gE4kMjZcDx9WxXtVISRDihe8DfEY"  # ВСТАВЬТЕ ТОКЕН!

async def main():
    bot = Bot(token=TOKEN)
    info = await bot.get_webhook_info()
    print(f"URL: {info.url}")
    print(f"Last error: {info.last_error_date} - {info.last_error_message}")
    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())