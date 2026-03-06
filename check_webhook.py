import sys
import asyncio
from aiogram import Bot

# Для Windows обязательно!
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

TOKEN = "8291526907:AAGT_l9gE4kMjZcDx9WxXtVISRDihe8DfEY"  # ВСТАВЬТЕ СВОЙ ТОКЕН

async def main():
    bot = Bot(token=TOKEN)
    webhook_info = await bot.get_webhook_info()
    print("Текущий webhook:")
    print(f"URL: {webhook_info.url}")
    print(f"Дата последней ошибки: {webhook_info.last_error_date}")
    print(f"Сообщение об ошибке: {webhook_info.last_error_message}")
    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())