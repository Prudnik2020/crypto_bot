# flask_app.py
import os
import asyncio
import logging
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from bot_core import bot, dp, handle_webhook, start_background_tasks

load_dotenv()

# Настраиваем логгирование
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# Глобальный event loop для asyncio
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# Запускаем фоновые задачи при старте
loop.run_until_complete(start_background_tasks())

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        update_json = request.get_json()
        if not update_json:
            return jsonify({'ok': False, 'error': 'No data'}), 400
        # Обрабатываем в asyncio цикле
        asyncio.run_coroutine_threadsafe(handle_webhook(update_json), loop)
        return jsonify({'ok': True})
    except Exception as e:
        logging.error(f"Error in webhook: {e}")
        return jsonify({'ok': False}), 500

@app.route('/', methods=['GET'])
def index():
    return "Bot is running!"

if __name__ == '__main__':
    # Для локального тестирования
    app.run(host='0.0.0.0', port=5000)