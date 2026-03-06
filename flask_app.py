import os
import logging
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

from bot_core import handle_update

app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    logging.info("Webhook received")
    try:
        update_json = request.get_json()
        if not update_json:
            return jsonify({'ok': False, 'error': 'No data'}), 400
        handle_update(update_json)
        return jsonify({'ok': True})
    except Exception as e:
        logging.error(f"Error in webhook: {e}", exc_info=True)
        return jsonify({'ok': False}), 500

@app.route('/', methods=['GET'])
def index():
    return "Bot is running!"