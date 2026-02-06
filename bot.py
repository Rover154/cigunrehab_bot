import os
import asyncio
import logging
from pathlib import Path
import json
from datetime import datetime

import openai
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
)

# === Логи ===
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# === Константы состояний ===
(
    ASK_NAME, ASK_AGE, ASK_HEIGHT_WEIGHT,
    ASK_DIAGNOSES_SELECTION, ASK_DIAGNOSIS_TIMING,
    ASK_MOBILITY, ASK_WELLBEING, GENERATE_COMPLEX,
) = range(8)

# === Переменные окружения ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
IO_NET_API_KEY = os.getenv("IO_NET_API_KEY")
ADMIN_TELEGRAM = os.getenv("ADMIN_TELEGRAM", "@cigunrehab")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "123456789"))

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN не задан!")

openai.api_key = IO_NET_API_KEY
openai.base_url = "https://api.intelligence.io.solutions/api/v1"

# === Хранение данных ===
DATA_FILE = Path("/tmp/users_data.json")

def load_profiles():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки профилей: {e}")
    return {}

def save_profiles(profiles):
    try:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(profiles, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}")

# === Клавиатуры (оставляем как у тебя) ===
# ... (все функции get_diagnosis_selection_keyboard, get_mobility_keyboard и т.д.) ...

# === Хендлеры (start, ask_name, ..., generate_complex, handle_message, handle_feedback_callback) ===
# ... (полностью как в последней версии, которую я тебе давал) ...

# === MAIN ===
async def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Добавляем хендлеры
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name)],
            ASK_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_age)],
            ASK_HEIGHT_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_height_weight)],
            ASK_DIAGNOSES_SELECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_diagnoses_selection)],
            ASK_DIAGNOSIS_TIMING: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_diagnosis_timing)],
            ASK_MOBILITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_mobility)],
            ASK_WELLBEING: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_wellbeing)],
            GENERATE_COMPLEX: [MessageHandler(filters.TEXT & ~filters.COMMAND, generate_complex)],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_feedback_callback))

    # Webhook настройки
    port = int(os.environ.get("PORT", 10000))
    webhook_url = f"https://{os.environ['RENDER_EXTERNAL_HOSTNAME']}/{TELEGRAM_TOKEN}"

    logger.info(f"Устанавливаем webhook: {webhook_url}")

    await application.bot.set_webhook(url=webhook_url)

    await application.initialize()
    await application.start()

    await application.updater.start_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TELEGRAM_TOKEN,
        webhook_url=webhook_url,
    )

    logger.info("Бот запущен и слушает webhook!")
    await asyncio.Event().wait()  # Держим процесс живым


if __name__ == "__main__":
    asyncio.run(main())