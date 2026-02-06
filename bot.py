import openai
import json
import csv
from datetime import datetime
from pathlib import Path
from io import StringIO
import os
import asyncio
from flask import Flask, request
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler
)

# === КОНСТАНТЫ СОСТОЯНИЙ ОПРОСНИКА (11 состояний) ===
(ASK_NAME, ASK_AGE, ASK_HEIGHT_WEIGHT, 
 ASK_DIAGNOSES_SELECTION, ASK_DIAGNOSIS_TIMING, 
 ASK_MOBILITY, ASK_WELLBEING, GENERATE_COMPLEX,
 ASK_FEEDBACK, BROADCAST_WAITING, MESSAGE_WAITING) = range(11)

# === ЗАГРУЗКА ТОКЕНОВ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
IO_NET_API_KEY = os.getenv("IO_NET_API_KEY")
ADMIN_TELEGRAM = os.getenv("ADMIN_TELEGRAM", "@cigunrehab")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "123456789"))

openai.api_key = IO_NET_API_KEY
openai.api_base = "https://api.intelligence.io.solutions/api/v1"

# === ХРАНИЛИЩЕ ПРОФИЛЕЙ ===
DATA_FILE = Path("/tmp/users_data.json")

def load_profiles():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Ошибка загрузки профилей: {e}")
            return {}
    return {}

def save_profiles(profiles):
    try:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(profiles, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ Ошибка сохранения профилей: {e}")

# === КЛАВИАТУРЫ ===
def get_diagnosis_selection_keyboard(selected=None):
    if selected is None:
        selected = []
    
    diagnoses = [
        ("🩺 Инсульт", "инсульт"),
        ("❤️ Инфаркт", "инфаркт"),
        ("🦴 Травма", "травма"),
        ("😰 Стресс", "стресс"),
        ("❓ Другое", "другое")
    ]
    
    buttons = []
    for label, value in diagnoses:
        if value in selected:
            buttons.append([f"{label} ✓"])
        else:
            buttons.append([label])
    
    buttons.append(["✅ Выбрал(а) всё"])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def get_mobility_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🛏️ Лежачий (не могу сидеть без поддержки)"],
            ["🪑 Сидячий (могу сидеть, но не могу стоять)"],
            ["🪑➡️ Стоячий с опорой (1-2 мин с опорой)"],
            ["🚶 Полноценная подвижность"]
        ],
        one_time_keyboard=True,
        resize_keyboard=True
    )

def get_main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🧘 Новый комплекс (новый опрос)"],
            ["👤 Мой профиль"],
            ["👨‍🏫 К инструктору"]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие"
    )

def get_feedback_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👍 Улучшилось", callback_data="feedback_good"),
            InlineKeyboardButton("😐 Без изменений", callback_data="feedback_neutral"),
            InlineKeyboardButton("👎 Ухудшилось", callback_data="feedback_bad")
        ],
        [
            InlineKeyboardButton("💬 Рассказать подробнее", callback_data="feedback_details")
        ]
    ])

# === ОПРОСНИК ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    
    await update.message.reply_text(
        "🌿 Добро пожаловать в Цигун-Реабилитацию!\n\n"
        "Пройдите короткий опрос (3 минуты) — и я составлю БЕЗОПАСНЫЙ комплекс "
        "с учётом ваших ограничений подвижности и диагнозов:",
        reply_markup=ReplyKeyboardRemove()
    )
    await update.message.reply_text("Как вас зовут?")
    return ASK_NAME

async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("Имя должно быть от 2 символов. Попробуйте ещё раз:")
        return ASK_NAME
    
    context.user_data["profile"] = {"name": name, "diagnoses": []}
    await update.message.reply_text(f"Приятно познакомиться, {name}! Сколько вам лет?")
    return ASK_AGE

async def ask_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        age = int(update.message.text.strip())
        if age < 5 or age > 120:
            raise ValueError
        context.user_data["profile"]["age"] = age
        await update.message.reply_text(
            f"Возраст: {age} лет.\nУкажите рост (см) и вес (кг) через пробел (пример: 170 75):"
        )
        return ASK_HEIGHT_WEIGHT
    except:
        await update.message.reply_text("Введите корректный возраст (число от 5 до 120):")
        return ASK_AGE

async def ask_height_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        parts = update.message.text.strip().split()
        height = int(parts[0])
        weight = int(parts[1])
        if height < 50 or height > 250 or weight < 10 or weight > 300:
            raise ValueError
        context.user_data["profile"]["height"] = height
        context.user_data["profile"]["weight"] = weight
        await update.message.reply_text(
            "Выберите ВСЕ подходящие диагнозы (можно несколько).\n"
            "Нажимайте кнопки по очереди — выбранные будут отмечены галочкой ✓:",
            reply_markup=get_diagnosis_selection_keyboard()
        )
        return ASK_DIAGNOSES_SELECTION
    except:
        await update.message.reply_text("Введите рост и вес числами через пробел (пример: 170 75):")
        return ASK_HEIGHT_WEIGHT

async def ask_diagnoses_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    diagnosis_map = {
        "🩺 Инсульт": "инсульт",
        "❤️ Инфаркт": "инфаркт",
        "🦴 Травма": "травма",
        "😰 Стресс": "стресс",
        "❓ Другое": "другое",
        "🩺 Инсульт ✓": "инсульт",
        "❤️ Инфаркт ✓": "инфаркт",
        "🦴 Травма ✓": "травма",
        "😰 Стресс ✓": "стресс",
        "❓ Другое ✓": "другое"
    }
    
    if text == "✅ Выбрал(а) всё":
        if not context.user_data["profile"]["diagnoses"]:
            await update.message.reply_text(
                "⚠️ Выберите хотя бы один диагноз:",
                reply_markup=get_diagnosis_selection_keyboard()
            )
            return ASK_DIAGNOSES_SELECTION
        
        context.user_data["diagnosis_index"] = 0
        return await ask_diagnosis_timing(update, context)
    
    diagnosis = diagnosis_map.get(text)
    if diagnosis:
        diagnoses_list = context.user_data["profile"]["diagnoses"]
        if diagnosis in diagnoses_list:
            diagnoses_list.remove(diagnosis)
        else:
            diagnoses_list.append(diagnosis)
        
        selected_text = ", ".join(diagnoses_list) if diagnoses_list else "ничего"
        await update.message.reply_text(
            f"Выбрано: {selected_text}\nДобавьте ещё или нажмите «✅ Выбрал(а) всё»:",
            reply_markup=get_diagnosis_selection_keyboard(diagnoses_list)
        )
        return ASK_DIAGNOSES_SELECTION
    
    await update.message.reply_text(
        "Выберите диагноз из кнопок ниже:",
        reply_markup=get_diagnosis_selection_keyboard(context.user_data["profile"]["diagnoses"])
    )
    return ASK_DIAGNOSES_SELECTION

async def ask_diagnosis_timing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    diagnoses = context.user_data["profile"]["diagnoses"]
    idx = context.user_data.get("diagnosis_index", 0)
    
    if idx >= len(diagnoses):
        await update.message.reply_text(
            "❗ КРИТИЧЕСКИ ВАЖНЫЙ ВОПРОС:\nКакова ваша подвижность сейчас?",
            reply_markup=get_mobility_keyboard()
        )
        return ASK_MOBILITY
    
    diagnosis = diagnoses[idx]
    ru_names = {
        "инсульт": "инсульт",
        "инфаркт": "инфаркт",
        "травма": "травма",
        "стресс": "стресс",
        "другое": "другая проблема"
    }
    
    await update.message.reply_text(
        f"Когда было событие «{ru_names.get(diagnosis, diagnosis)}»?\n"
        "(пример: «3 месяца назад», «неделю назад», «2 года назад»)"
    )
    context.user_data["current_diagnosis"] = diagnosis
    return ASK_DIAGNOSIS_TIMING

async def save_diagnosis_timing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    timing = update.message.text.strip()
    diagnosis = context.user_data["current_diagnosis"]
    
    if "diagnoses_details" not in context.user_data["profile"]:
        context.user_data["profile"]["diagnoses_details"] = []
    
    context.user_data["profile"]["diagnoses_details"].append({
        "type": diagnosis,
        "timing": timing
    })
    
    context.user_data["diagnosis_index"] = context.user_data.get("diagnosis_index", 0) + 1
    return await ask_diagnosis_timing(update, context)

async def ask_mobility(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    mobility_map = {
        "🛏️ Лежачий (не могу сидеть без поддержки)": "лежачий",
        "🪑 Сидячий (могу сидеть, но не могу стоять)": "сидячий",
        "🪑➡️ Стоячий с опорой (1-2 мин с опорой)": "стоячий_с_опорой",
        "🚶 Полноценная подвижность": "полноценная"
    }
    
    mobility = mobility_map.get(text)
    if not mobility:
        await update.message.reply_text(
            "Выберите вариант подвижности из кнопок:",
            reply_markup=get_mobility_keyboard()
        )
        return ASK_MOBILITY
    
    context.user_data["profile"]["mobility"] = mobility
    await update.message.reply_text(
        "Кратко опишите самочувствие и ограничения:\n"
        "(пример: «головокружение при вставании», «слабость в правой руке», «усталость к вечеру»)"
    )
    return ASK_WELLBEING

async def ask_wellbeing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["profile"]["wellbeing"] = update.message.text.strip()
    context.user_data["profile"]["completed"] = True
    context.user_data["profile"]["registered_at"] = update.message.date.isoformat()
    context.user_data["profile"]["next_reminder_days"] = [3, 7, 14]
    context.user_data["profile"]["last_reminder_sent"] = None
    
    user_id = str(update.effective_user.id)
    profiles = load_profiles()
    is_new_client = user_id not in profiles
    
    profiles[user_id] = context.user_data["profile"]
    save_profiles(profiles)
    
    # Уведомление админа
    if is_new_client:
        try:
            diagnoses_summary = ", ".join([
                f"{d['type']} ({d['timing']})" 
                for d in context.user_data["profile"].get("diagnoses_details", [])
            ]) or "не указаны"
            
            mobility_ru = {
                "лежачий": "🛏️ ЛЕЖАЧИЙ",
                "сидячий": "🪑 СИДЯЧИЙ",
                "стоячий_с_опорой": "🪑➡️ С ОПОРОЙ",
                "полноценная": "🚶 ПОЛНОЦЕННАЯ"
            }
            
            admin_message = (
                f"🆕 НОВЫЙ КЛИЕНТ в боте Цигун-Реабилитация!\n\n"
                f"Имя: {context.user_data['profile']['name']}\n"
                f"Возраст: {context.user_data['profile']['age']} лет\n"
                f"Диагнозы: {diagnoses_summary}\n"
                f"Подвижность: {mobility_ru.get(context.user_data['profile']['mobility'], context.user_data['profile']['mobility'])}\n"
                f"Telegram ID: {user_id}\n"
                f"Зарегистрирован: {update.message.date.strftime('%d.%m.%Y %H:%M')}\n\n"
                f"❗ Проверьте профиль: /new_clients"
            )
            
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_message)
            print(f"✅ Уведомление админу отправлено о новом клиенте {user_id}")
        except Exception as e:
            print(f"⚠️ Не удалось отправить уведомление админу: {e}")
    
    await update.message.reply_text(
        "✅ Опрос завершён! Анализирую данные и составляю БЕЗОПАСНЫЙ комплекс упражнений...",
        reply_markup=ReplyKeyboardRemove()
    )
    return await generate_complex(update, context)

async def generate_complex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = context.user_data.get("profile", {})
    
    diagnoses_text = []
    for d in profile.get("diagnoses_details", []):
        diagnoses_text.append(f"• {d['type']}: {d['timing']}")
    
    mobility_map_ru = {
        "лежачий": "ЛЕЖАЧИЙ (только упражнения лёжа)",
        "сидячий": "СИДЯЧИЙ (только сидячие упражнения)",
        "стоячий_с_опорой": "СТОЯЧИЙ С ОПОРОЙ (кратковременные стоячие упражнения с опорой)",
        "полноценная": "ПОЛНОЦЕННАЯ подвижность"
    }
    
    profile_info = (
        f"Имя: {profile.get('name', 'не указано')}, "
        f"Возраст: {profile.get('age', '?')} лет\n"
        f"Диагнозы:\n" + "\n".join(diagnoses_text) + "\n"
        f"Подвижность: {mobility_map_ru.get(profile.get('mobility'), profile.get('mobility'))}\n"
        f"Самочувствие: {profile.get('wellbeing', 'не указано')}"
    )
    
    thinking_msg = await update.message.reply_text("Практикую осознанность... 🧘‍♂️")
    
    try:
        response = openai.ChatCompletion.create(
            model="moonshotai/Kimi-K2-Instruct-0905",
            messages=[
                {"role": "system", "content": f"""Вы — инструктор по цигун для реабилитации. Составляете БЕЗОПАСНЫЕ комплексы с учётом ограничений подвижности.

ПРОФИЛЬ ПАЦИЕНТА:
{profile_info}

КРИТИЧЕСКИЕ ПРАВИЛА БЕЗОПАСНОСТИ:
1. ЕСЛИ ПАЦИЕНТ ЛЕЖАЧИЙ → ТОЛЬКО упражнения лёжа
2. ЕСЛИ СИДЯЧИЙ → ТОЛЬКО сидячие упражнения
3. ЕСЛИ СТОЯЧИЙ С ОПОРОЙ → короткие стоячие упражнения (макс. 1-2 мин) ТОЛЬКО с опорой
4. Для инсульта/инфаркта: избегать резких движений, упор на дыхание

СТРУКТУРА КОМПЛЕКСА:
• Название упражнения
• Положение тела
• Дыхание
• Движения
• Длительность

ОБЯЗАТЕЛЬНО В КОНЦЕ:
«❗ Обязательно проконсультируйтесь с лечащим врачом перед практикой.
Для детального комплекса напишите инструктору: {ADMIN_TELEGRAM}»
Отвечайте кратко (до 250 слов), только на русском."""},
                {"role": "user", "content": "Составь безопасный комплекс цигун для реабилитации с учётом всех ограничений подвижности."}
            ],
            max_tokens=450,
            temperature=0.5,
            top_p=0.9,
        )
        
        ai_reply = response.choices[0].message.content.strip()
        
        try:
            await thinking_msg.delete()
        except:
            pass
        
        if "врач" not in ai_reply.lower() and "консульт" not in ai_reply.lower():
            ai_reply += "\n\n❗ Обязательно проконсультируйтесь с лечащим врачом перед практикой."
        
        if ADMIN_TELEGRAM not in ai_reply:
            ai_reply += f"\n\nДля детального комплекса напишите инструктору: {ADMIN_TELEGRAM}"
        
        await update.message.reply_text(ai_reply, reply_markup=get_main_menu_keyboard())
        return ConversationHandler.END
        
    except Exception as e:
        try:
            await thinking_msg.delete()
        except:
            pass
        
        await update.message.reply_text(
            f"😔 Не удалось составить комплекс. Попробуйте позже или напишите инструктору: {ADMIN_TELEGRAM}",
            reply_markup=get_main_menu_keyboard()
        )
        print(f"Ошибка генерации: {e}")
        return ConversationHandler.END

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    if text == "🧘 Новый комплекс (новый опрос)":
        return await start(update, context)
    
    elif text == "👤 Мой профиль":
        user_id = str(update.effective_user.id)
        profiles = load_profiles()
        profile = profiles.get(user_id, {})
        
        if not profile.get("completed"):
            await update.message.reply_text("Сначала пройдите опрос командой /start", reply_markup=get_main_menu_keyboard())
            return
        
        diagnoses_text = "\n".join([f"  • {d['type']}: {d['timing']}" for d in profile.get("diagnoses_details", [])]) or "  не указаны"
        
        mobility_ru = {
            "лежачий": "🛏️ Лежачий",
            "сидячий": "🪑 Сидячий",
            "стоячий_с_опорой": "🪑➡️ Стоячий с опорой",
            "полноценная": "🚶 Полноценная подвижность"
        }
        
        text = (
            "👤 Ваш профиль:\n\n"
            f"Имя: {profile.get('name', '-')}\n"
            f"Возраст: {profile.get('age', '-')} лет\n"
            f"Рост: {profile.get('height', '-')} см, вес: {profile.get('weight', '-')} кг\n"
            f"Диагнозы:\n{diagnoses_text}\n"
            f"Подвижность: {mobility_ru.get(profile.get('mobility'), profile.get('mobility', '-'))}\n"
            f"Самочувствие: {profile.get('wellbeing', '-')[:100]}..."
        )
        await update.message.reply_text(text, reply_markup=get_main_menu_keyboard())
        return
    
    elif text == "👨‍🏫 К инструктору":
        await update.message.reply_text(
            f"👨‍🏫 Для глубокой персонализации напишите инструктору:\n{ADMIN_TELEGRAM}",
            reply_markup=get_main_menu_keyboard()
        )
        return
    
    await update.message.reply_text(
        "Для получения комплекса упражнений начните опрос командой /start",
        reply_markup=get_main_menu_keyboard()
    )

async def handle_feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    profiles = load_profiles()
    profile = profiles.get(user_id, {})
    
    if not profile.get("completed"):
        await query.edit_message_text("Сначала пройдите опрос (/start)")
        return
    
    feedback_map = {
        "feedback_good": "улучшилось",
        "feedback_neutral": "без изменений",
        "feedback_bad": "ухудшилось"
    }
    
    feedback_type = feedback_map.get(query.data, "неизвестно")
    
    if "feedback_history" not in profile:
        profile["feedback_history"] = []
    
    profile["feedback_history"].append({
        "date": datetime.now().isoformat(),
        "type": feedback_type,
        "days_since_registration": (datetime.now() - datetime.fromisoformat(profile["registered_at"].replace("Z", "+00:00"))).days
    })
    
    profiles[user_id] = profile
    save_profiles(profiles)
    
    if query.data == "feedback_good":
        response_text = f"🌟 Отлично! Для персонализированной программы напишите {ADMIN_TELEGRAM}"
    elif query.data == "feedback_neutral":
        response_text = f"🧘 Главное — регулярность! Напишите {ADMIN_TELEGRAM} для подбора упражнений"
    elif query.data == "feedback_bad":
        response_text = f"😔 Проконсультируйтесь с врачом. Инструктор поможет адаптировать практики: {ADMIN_TELEGRAM}"
    else:
        response_text = f"💬 Напишите подробнее инструктору: {ADMIN_TELEGRAM}"
    
    await query.edit_message_text(text=response_text, reply_markup=get_main_menu_keyboard())

# === Flask приложение для вебхуков ===
app = Flask(__name__)

# Глобальная переменная для хранения приложения
bot_application = None

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    global bot_application
    
    if request.headers.get("content-type") == "application/json":
        try:
            # Получаем данные как словарь, а не строку
            json_dict = request.get_json()
            
            # Создаём объект Update из словаря
            update = Update.de_json(json_dict, bot_application.bot)
            
            # Помещаем обновление в очередь бота
            asyncio.run(bot_application.update_queue.put(update))
            
            return "OK"
        except Exception as e:
            print(f"Ошибка обработки вебхука: {e}")
            return "Error", 500
    else:
        return "Invalid content-type", 403

@app.route("/", methods=["GET"])
def index():
    return "Cigunrehab Bot is running! 🌿", 200

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

# === ЗАПУСК ПРИЛОЖЕНИЯ ===
if __name__ == "__main__":
    print("="*70)
    print("🌿 ЦИГУН-РЕАБИЛИТАЦИЯ (вебхуки)")
    print("✅ Работает как бесплатный Web Service на Render.com")
    print("="*70)
    
    # Инициализация бота
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # ConversationHandler
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
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
        allow_reentry=True,
    )
    
    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_feedback_callback))
    
    # Сохраняем приложение в глобальной переменной для доступа из вебхука
    bot_application = application
    
    # Запуск приложения (асинхронно)
    PORT = int(os.environ.get("PORT", 10000))
    RENDER_EXTERNAL_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "localhost")
    WEBHOOK_URL = f"https://{RENDER_EXTERNAL_HOSTNAME}/{TELEGRAM_TOKEN}"
    
    print(f"🌐 Webhook URL: {WEBHOOK_URL}")
    print(f"🚪 Порт: {PORT}")
    
    # Устанавливаем вебхук
    import threading
    
    def setup_webhook():
        asyncio.run(application.bot.set_webhook(url=WEBHOOK_URL))
        print("✅ Вебхук успешно установлен!")
    
    # Запускаем установку вебхука в отдельном потоке
    webhook_thread = threading.Thread(target=setup_webhook)
    webhook_thread.start()
    
    # Запускаем приложение в фоновом режиме
    application.updater = None
    application.run_polling(
        drop_pending_updates=True,
        close_loop=False
    )
    
    # Запуск Flask (этот код не выполнится, так как run_polling блокирует поток)
    # Но на Render Flask запускается отдельно через WSGI сервер