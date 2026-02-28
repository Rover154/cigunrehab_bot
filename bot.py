import os
import logging
from pathlib import Path
import json
from datetime import datetime
import openai
import requests
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
from urllib.parse import unquote
import base64
import json

# === Логи ===
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# === Константы состояний ===
(
    ASK_NAME,
    ASK_AGE,
    ASK_HEIGHT_WEIGHT,
    ASK_DIAGNOSES_SELECTION,
    ASK_DIAGNOSIS_TIMING,
    ASK_MOBILITY,
    ASK_WELLBEING,
    GENERATE_COMPLEX,
) = range(8)

# === Переменные окружения ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
IO_NET_API_KEY = os.getenv("IO_NET_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "gsk_ZgESNwBSNvMbYpes3ysDWGdyb3FY6EaCXy2DRwMnMjtndwxDUDr").strip()
ADMIN_TELEGRAM = os.getenv("ADMIN_TELEGRAM", "@cigunrehab").strip()
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "6810836580").strip())

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN не задан!")

# === Настройка OpenAI для io.net ===
openai.api_key = IO_NET_API_KEY
openai.api_base = "https://api.intelligence.io.solutions/api/v1"

# === Глобальные переменные для переключения API ===
USE_GROQ = False  # Флаг: False = io.net, True = Groq
IO_NET_DAILY_LIMIT = 10000  # Дневной лимит токенов для io.net
GROQ_DAILY_LIMIT = 100000  # Дневной лимит для Groq (100k токенов)
io_net_tokens_used = 0
groq_tokens_used = 0
io_net_rate_limit_reset = None  # Время сброса лимита io.net
groq_rate_limit_reset = None  # Время сброса лимита Groq
last_reset_date = None  # Дата последнего сброса счётчиков


def reset_daily_tokens_if_new_day():
    """Сброс счётчиков токенов при наступлении нового дня"""
    global last_reset_date, io_net_tokens_used, groq_tokens_used
    
    today = datetime.now().date()
    if last_reset_date != today:
        logger.info(f"Новый день ({today}), сброс счётчиков токенов")
        io_net_tokens_used = 0
        groq_tokens_used = 0
        last_reset_date = today


def check_io_net_tokens():
    """
    Проверка доступности io.net API и остатка токенов.
    Проверяем через headers rate limit (X-RateLimit-Remaining, X-RateLimit-Reset).
    """
    global USE_GROQ, io_net_tokens_used, io_net_rate_limit_reset

    # Проверяем, не наступил ли новый день
    reset_daily_tokens_if_new_day()
    
    try:
        response = requests.get(
            "https://api.intelligence.io.solutions/api/v1/models",
            headers={"Authorization": f"Bearer {IO_NET_API_KEY}"},
            timeout=10
        )
        
        # Проверяем headers с лимитами
        remaining = response.headers.get('X-RateLimit-Remaining')
        reset_time = response.headers.get('X-RateLimit-Reset')
        
        if reset_time:
            try:
                io_net_rate_limit_reset = int(reset_time)
            except ValueError:
                pass
        
        if response.status_code == 401:
            logger.error("io.net: Неверный API ключ")
            return False
        elif response.status_code == 429:
            logger.warning("io.net: Превышен лимит токенов (429), переключаемся на Groq")
            return False
        elif response.status_code == 200:
            # Проверяем остаток токенов в headers
            if remaining is not None:
                remaining_tokens = int(remaining)
                logger.info(f"io.net: Осталось токенов в лимите: {remaining_tokens}")
                if remaining_tokens <= 0:
                    logger.warning("io.net: Лимит токенов исчерпан, переключаемся на Groq")
                    return False
            logger.info("io.net: API доступно")
            return True
        else:
            logger.warning(f"io.net: Статус {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"io.net: Ошибка проверки — {e}")
        return False


def check_groq_tokens():
    """
    Проверка доступности Groq API.
    Groq возвращает headers: x-ratelimit-remaining-tokens, x-ratelimit-reset-tokens
    """
    global GROQ_API_KEY, groq_rate_limit_reset

    try:
        response = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            timeout=10
        )
        
        # Groq возвращает headers с лимитами
        remaining_tokens = response.headers.get('x-ratelimit-remaining-tokens')
        reset_time = response.headers.get('x-ratelimit-reset-tokens')
        
        if reset_time:
            try:
                groq_rate_limit_reset = int(reset_time)
            except ValueError:
                pass
        
        if response.status_code == 401:
            logger.error("Groq: Неверный API ключ")
            return False
        elif response.status_code == 429:
            logger.warning("Groq: Превышен лимит токенов (429)")
            return False
        elif response.status_code == 200:
            # Проверяем остаток токенов
            if remaining_tokens is not None:
                remaining = int(remaining_tokens)
                logger.info(f"Groq: Осталось токенов в лимите: {remaining}")
                if remaining <= 0:
                    logger.warning("Groq: Лимит токенов исчерпан, переключаемся на io.net")
                    return False
            logger.info("Groq: API доступно")
            return True
        else:
            logger.warning(f"Groq: Статус {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"Groq: Ошибка проверки — {e}")
        return False


def get_available_api():
    """Определение доступного API с приоритетом io.net"""
    global USE_GROQ

    # Сначала пробуем io.net (основной)
    if not USE_GROQ and check_io_net_tokens():
        return "io_net"

    # Если io.net недоступен, пробуем Groq
    if check_groq_tokens():
        USE_GROQ = True
        return "groq"

    # Если Groq тоже недоступен, пробуем снова io.net
    if check_io_net_tokens():
        USE_GROQ = False
        return "io_net"

    return None


def generate_with_fallback(messages, max_tokens=500, temperature=0.5, top_p=0.9, retry_count=0):
    """
    Генерация текста с авто-переключением между API.
    При исчерпании лимита токенов автоматически переключается на альтернативный API.
    """
    global USE_GROQ, io_net_tokens_used, groq_tokens_used, io_net_rate_limit_reset, groq_rate_limit_reset

    # Защита от бесконечной рекурсии
    if retry_count > 3:
        logger.error("Превышено количество попыток переключения API")
        return "😔 Сервис временно недоступен. Попробуйте позже."

    api = get_available_api()
    logger.info(f"Используем API: {api}")

    if api == "groq":
        # Генерация через Groq (llama-3.1-8b-instant)
        try:
            groq_client = openai.OpenAI(
                api_key=GROQ_API_KEY,
                base_url="https://api.groq.com/openai/v1"
            )
            response = groq_client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
            )
            
            # Подсчёт токенов из ответа
            usage = response.usage
            tokens_used = usage.total_tokens if usage else 0
            groq_tokens_used += tokens_used
            logger.info(f"Groq: использовано токенов: {tokens_used}, всего сегодня: {groq_tokens_used}")

            # Проверка дневного лимита Groq
            if groq_tokens_used >= GROQ_DAILY_LIMIT:
                logger.warning("Groq: Достигнут дневной лимит, пробуем io.net")
                USE_GROQ = False
                groq_tokens_used = 0
                # Пробуем переключиться на io.net
                return generate_with_fallback(messages, max_tokens, temperature, top_p, retry_count + 1)

            return response.choices[0].message.content.strip()
            
        except openai.error.RateLimitError as e:
            logger.warning(f"Groq: Rate limit error — {e}")
            # Переключаемся на io.net
            USE_GROQ = False
            return generate_with_fallback(messages, max_tokens, temperature, top_p, retry_count + 1)
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Groq ошибка: {e}")
            # Проверяем, не ошибка ли это лимита (429)
            if "429" in error_msg or "rate limit" in error_msg.lower():
                USE_GROQ = False
                return generate_with_fallback(messages, max_tokens, temperature, top_p, retry_count + 1)
            # Пробуем io.net
            USE_GROQ = False
            return generate_with_fallback(messages, max_tokens, temperature, top_p, retry_count + 1)

    else:
        # Генерация через io.net (Kimi-K2)
        try:
            openai.api_key = IO_NET_API_KEY
            openai.api_base = "https://api.intelligence.io.solutions/api/v1"

            response = openai.ChatCompletion.create(
                model="kimi-k2",
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
            )
            
            # Подсчёт токенов из ответа
            usage = response.get('usage', {})
            tokens_used = usage.get('total_tokens', 0)
            io_net_tokens_used += tokens_used
            logger.info(f"io.net: использовано токенов: {tokens_used}, всего сегодня: {io_net_tokens_used}")

            # Проверка дневного лимита io.net
            if io_net_tokens_used >= IO_NET_DAILY_LIMIT:
                logger.warning("io.net: Достигнут дневной лимит, пробуем Groq")
                USE_GROQ = True
                io_net_tokens_used = 0
                # Пробуем переключиться на Groq
                return generate_with_fallback(messages, max_tokens, temperature, top_p, retry_count + 1)

            return response.choices[0].message.content.strip()
            
        except openai.error.RateLimitError as e:
            logger.warning(f"io.net: Rate limit error — {e}")
            # Переключаемся на Groq
            USE_GROQ = True
            return generate_with_fallback(messages, max_tokens, temperature, top_p, retry_count + 1)
        except Exception as e:
            error_msg = str(e)
            logger.error(f"io.net ошибка: {e}")
            # Проверяем, не ошибка ли это лимита (429)
            if "429" in error_msg or "rate limit" in error_msg.lower():
                USE_GROQ = True
                return generate_with_fallback(messages, max_tokens, temperature, top_p, retry_count + 1)
            # Пробуем Groq
            USE_GROQ = True
            return generate_with_fallback(messages, max_tokens, temperature, top_p, retry_count + 1)

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

# === Клавиатуры ===
def get_diagnosis_selection_keyboard(selected=None):
    if selected is None:
        selected = []
    diagnoses = [
        ("🩺 Инсульт", "инсульт"),
        ("❤️ Инфаркт", "инфаркт"),
        ("🦴 Травма", "травма"),
        ("😰 Стресс", "стресс"),
        ("❓ Другое", "другое"),
    ]
    buttons = []
    for label, value in diagnoses:
        if value in selected:
            buttons.append([f"{label} ✓"])
        else:
            buttons.append([label])
    buttons.append(["Продолжить"])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def get_mobility_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🛏️ Лежачий (не могу сидеть без поддержки)"],
            ["🪑 Сидячий (могу сидеть, но не могу стоять)"],
            ["🪑➡️ Стоячий с опорой (1-2 мин с опорой)"],
            ["🚶 Полноценная подвижность"],
        ],
        one_time_keyboard=True,
        resize_keyboard=True,
    )

def get_main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🧘 Новый комплекс (новый опрос)"],
            ["📋 Получить полный комплекс"],
            ["👤 Мой профиль"],
            ["👨‍🏫 К инструктору"],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )

def get_feedback_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👍 Улучшилось", callback_data="feedback_good"),
            InlineKeyboardButton("😐 Без изменений", callback_data="feedback_neutral"),
            InlineKeyboardButton("👎 Ухудшилось", callback_data="feedback_bad"),
        ],
        [
            InlineKeyboardButton("💬 Рассказать подробнее", callback_data="feedback_details"),
        ],
    ])

# === ОПРОСНИК ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    
    # === Обработка start-параметра с данными из приложения ===
    if context.args:
        try:
            start_param = context.args[0]
            logger.info(f"Получен start параметр: {start_param[:50]}...")
            
            # Декодируем base64
            try:
                # Добавляем padding если нужно
                padding = 4 - len(start_param) % 4
                if padding != 4:
                    start_param += '=' * padding
                decoded_bytes = base64.b64decode(start_param)
                decoded_param = decoded_bytes.decode('utf-8')
                data = json.loads(decoded_param)
            except Exception as decode_err:
                logger.error(f"Ошибка декодирования base64: {decode_err}")
                # Пробуем как обычный JSON
                data = json.loads(unquote(start_param))
            
            logger.info(f"Декодированные данные: {data}")
            
            # Сохраняем профиль из компактного формата
            profile = {
                "name": data.get("n", ""),
                "age": data.get("a", ""),
                "diagnoses": data.get("d", []),
                "time": data.get("t"),
                "symptoms": data.get("s", []),
                "format": data.get("f"),
                "completed": True,
                "from_app": True,
                "registered_at": datetime.now().isoformat(),
            }
            
            # Сохраняем профиль
            user_id = str(update.effective_user.id)
            profiles = load_profiles()
            is_new_client = user_id not in profiles
            profiles[user_id] = profile
            save_profiles(profiles)
            
            # Отправляем уведомление админу
            if is_new_client:
                try:
                    diagnosis_map = {
                        "stroke": "Инсульт",
                        "infarct": "Инфаркт",
                        "trauma": "Травма",
                        "stress": "Стресс",
                        "other": "Другое",
                    }
                    admin_message = (
                        f"🆕 НОВЫЙ КЛИЕНТ из приложения!\n\n"
                        f"Имя: {profile['name']}\n"
                        f"Возраст: {profile['age']} лет\n"
                        f"Диагнозы: {', '.join([diagnosis_map.get(d, d) for d in profile['diagnoses']]) if profile['diagnoses'] else 'не указаны'}\n"
                        f"Симптомы: {', '.join(profile['symptoms']) if profile['symptoms'] else 'не указаны'}\n"
                        f"Период: {profile['time']}\n"
                        f"Формат: {profile['format']}\n"
                        f"Telegram ID: {user_id}"
                    )
                    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_message)
                    logger.info("✅ Уведомление админу отправлено")
                except Exception as e:
                    logger.error(f"⚠️ Не удалось отправить уведомление админу: {e}")
            
            # Приветственное сообщение с данными из приложения
            name = profile['name'] if profile['name'] else update.effective_user.first_name
            await update.message.reply_text(
                f"🌿 Здравствуйте, {name}!\n\n"
                f"✅ Ваши данные получены:\n"
                f"• Возраст: {profile['age']} лет\n"
                f"• Диагнозы: {', '.join([diagnosis_map.get(d, d) for d in profile['diagnoses']]) if profile['diagnoses'] else 'не указаны'}\n\n"
                f"🧘 Сейчас составлю для вас персональный комплекс...",
                reply_markup=ReplyKeyboardRemove(),
            )
            
            # Генерируем комплекс
            return await generate_complex_from_app(update, context, profile)
            
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка декодирования start-параметра: {e}")
        except Exception as e:
            logger.error(f"Ошибка обработки start-параметра: {e}")
    
    # Стандартный поток - начало опроса
    await update.message.reply_text(
        "🌿 Добро пожаловать в Цигун-Реабилитацию!\n\n"
        "Пройдите короткий опрос (3 минуты) — и я составлю БЕЗОПАСНЫЙ комплекс "
        "с учётом ваших ограничений подвижности и диагнозов:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text("Как вас зовут?")
    return ASK_NAME

async def generate_complex_from_app(update: Update, context: ContextTypes.DEFAULT_TYPE, profile):
    """Генерация комплекса для данных из приложения"""

    # Маппинг диагнозов
    diagnosis_map = {
        "stroke": "Инсульт",
        "infarct": "Инфаркт",
        "trauma": "Травма",
        "stress": "Стресс/нервное перенапряжение",
        "other": "Другое",
    }

    # Маппинг симптомов
    symptom_map = {
        "pain": "Боль",
        "stiffness": "Скованность движений",
        "weakness": "Слабость",
        "dizziness": "Головокружение",
        "fatigue": "Быстрая утомляемость",
        "sleep": "Нарушения сна",
        "anxiety": "Тревожность",
        "other": "Другое",
    }

    # Маппинг периода
    time_map = {
        "acute": "Острый период (до 1 месяца)",
        "1-3": "1-3 месяца",
        "3-6": "3-6 месяцев",
        "6plus": "6-12 месяцев",
        "1yplus": "Более 1 года",
        "any": "Любой период",
    }

    # Формируем описание профиля для AI
    diagnoses_text = []
    for d in profile.get("diagnoses", []):
        diagnoses_text.append(f"• {diagnosis_map.get(d, d)}")

    symptoms_text = []
    for s in profile.get("symptoms", []):
        symptoms_text.append(f"• {symptom_map.get(s, s)}")

    # Определяем подвижность (упрощённо - по диагнозам)
    mobility = "полноценная"  # по умолчанию
    if "stroke" in profile.get("diagnoses", []) or "infarct" in profile.get("diagnoses", []):
        mobility = "стоячий_с_опорой"

    profile_info = (
        f"Имя: {profile.get('name', 'не указано')}, "
        f"Возраст: {profile.get('age', '?')} лет\n"
        f"Диагнозы:\n" + "\n".join(diagnoses_text) + "\n"
        f"Симптомы:\n" + "\n".join(symptoms_text) + "\n"
        f"Период заболевания: {time_map.get(profile.get('time', ''), profile.get('time', 'не указан'))}\n"
        f"Подвижность: {mobility}\n"
        f"Формат занятий: {profile.get('format', 'не указан')}"
    )

    thinking_msg = await update.message.reply_text("🧘 Практикую осознанность и составляю комплекс...")

    messages = [
        {
            "role": "system",
            "content": f"""Вы — инструктор по цигун для реабилитации. Составляете БЕЗОПАСНЫЕ комплексы с учётом ограничений подвижности.

ПРОФИЛЬ ПАЦИЕНТА: {profile_info}

КРИТИЧЕСКИЕ ПРАВИЛА БЕЗОПАСНОСТИ:
1. ЕСЛИ ПАЦИЕНТ ЛЕЖАЧИЙ → ТОЛЬКО упражнения лёжа
2. ЕСЛИ СИДЯЧИЙ → ТОЛЬКО сидячие упражнения
3. ЕСЛИ СТОЯЧИЙ С ОПОРОЙ → короткие стоячие упражнения (макс. 1-2 мин) ТОЛЬКО с опорой
4. Для инсульта/инфаркта: избегать резких движений, упор на дыхание
5. Учитывать все симптомы и противопоказания

СТРУКТУРА КОМПЛЕКСА:
• Название упражнения
• Положение тела
• Дыхание
• Движения
• Длительность/повторы

ОБЯЗАТЕЛЬНО В КОНЦЕ: «❗ Обязательно проконсультируйтесь с лечащим врачом перед практикой. Для детального комплекса напишите инструктору: {ADMIN_TELEGRAM}»

Отвечайте кратко (до 300 слов), только на русском. Выдай 3-5 базовых упражнений для бесплатной версии."""
        },
        {
            "role": "user",
            "content": "Составь безопасный комплекс цигун для реабилитации с учётом всех ограничений подвижности."
        },
    ]

    try:
        ai_reply = generate_with_fallback(messages, max_tokens=500, temperature=0.5, top_p=0.9)
        
        try:
            await thinking_msg.delete()
        except:
            pass
        
        # Добавляем предупреждение о враче если нет
        if "врач" not in ai_reply.lower() and "консульт" not in ai_reply.lower():
            ai_reply += "\n\n❗ Обязательно проконсультируйтесь с лечащим врачом перед практикой."
        
        if ADMIN_TELEGRAM not in ai_reply:
            ai_reply += f"\n\nДля полного комплекса (10-15 упражнений) напишите инструктору: {ADMIN_TELEGRAM}"
        
        # Добавляем кнопку для покупки полной версии
        await update.message.reply_text(
            ai_reply,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💰 Купить полную версию (299₽)", url="https://t.me/cigunrehab")
            ]])
        )
        
        return ConversationHandler.END
        
    except Exception as e:
        try:
            await thinking_msg.delete()
        except:
            pass
        logger.error(f"Ошибка генерации: {e}")
        await update.message.reply_text(
            f"😔 Не удалось составить комплекс. Попробуйте позже или напишите инструктору: {ADMIN_TELEGRAM}",
            reply_markup=get_main_menu_keyboard(),
        )
        return ConversationHandler.END

# ... остальные функции (ask_name, ask_age, etc.) остаются без изменений для стандартного опроса

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
            reply_markup=get_diagnosis_selection_keyboard(),
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
        "❓ Другое ✓": "другое",
    }
    if text == "Продолжить":
        if not context.user_data["profile"]["diagnoses"]:
            await update.message.reply_text(
                "⚠️ Выберите хотя бы один диагноз:",
                reply_markup=get_diagnosis_selection_keyboard(),
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
            f"Выбрано: {selected_text}\nДобавьте ещё или нажмите «Продолжить»:",
            reply_markup=get_diagnosis_selection_keyboard(diagnoses_list),
        )
        return ASK_DIAGNOSES_SELECTION
    await update.message.reply_text(
        "Выберите диагноз из кнопок ниже:",
        reply_markup=get_diagnosis_selection_keyboard(context.user_data["profile"]["diagnoses"]),
    )
    return ASK_DIAGNOSES_SELECTION

async def ask_diagnosis_timing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    diagnoses = context.user_data["profile"]["diagnoses"]
    idx = context.user_data.get("diagnosis_index", 0)
    if idx >= len(diagnoses):
        await update.message.reply_text(
            "❗ КРИТИЧЕСКИ ВАЖНЫЙ ВОПРОС:\nКакова ваша подвижность сейчас?",
            reply_markup=get_mobility_keyboard(),
        )
        return ASK_MOBILITY
    diagnosis = diagnoses[idx]
    ru_names = {
        "инсульт": "инсульт",
        "инфаркт": "инфаркт",
        "травма": "травма",
        "стресс": "стресс",
        "другое": "другая проблема",
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
        "timing": timing,
    })
    context.user_data["diagnosis_index"] = context.user_data.get("diagnosis_index", 0) + 1
    return await ask_diagnosis_timing(update, context)

async def ask_mobility(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    mobility_map = {
        "🛏️ Лежачий (не могу сидеть без поддержки)": "лежачий",
        "🪑 Сидячий (могу сидеть, но не могу стоять)": "сидячий",
        "🪑➡️ Стоячий с опорой (1-2 мин с опорой)": "стоячий_с_опорой",
        "🚶 Полноценная подвижность": "полноценная",
    }
    mobility = mobility_map.get(text)
    if not mobility:
        await update.message.reply_text(
            "Выберите вариант подвижности из кнопок:",
            reply_markup=get_mobility_keyboard(),
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
                "полноценная": "🚶 ПОЛНОЦЕННАЯ",
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
            logger.info(f"✅ Уведомление админу отправлено о новом клиенте {user_id}")
        except Exception as e:
            logger.error(f"⚠️ Не удалось отправить уведомление админу: {e}")
    await update.message.reply_text(
        "✅ Опрос завершён! Анализирую данные и составляю БЕЗОПАСНЫЙ комплекс упражнений...",
        reply_markup=ReplyKeyboardRemove(),
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
        "полноценная": "ПОЛНОЦЕННАЯ подвижность",
    }
    profile_info = (
        f"Имя: {profile.get('name', 'не указано')}, "
        f"Возраст: {profile.get('age', '?')} лет\n"
        f"Диагнозы:\n" + "\n".join(diagnoses_text) + "\n"
        f"Подвижность: {mobility_map_ru.get(profile.get('mobility'), profile.get('mobility'))}\n"
        f"Самочувствие: {profile.get('wellbeing', 'не указано')}"
    )
    thinking_msg = await update.message.reply_text("Практикую осознанность... 🧘‍♂️")
    
    messages = [
        {
            "role": "system",
            "content": f"""Вы — инструктор по цигун для реабилитации. Составляете БЕЗОПАСНЫЕ комплексы с учётом ограничений подвижности.

ПРОФИЛЬ ПАЦИЕНТА: {profile_info}

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

ОБЯЗАТЕЛЬНО В КОНЦЕ: «❗ Обязательно проконсультируйтесь с лечащим врачом перед практикой. Для детального комплекса напишите инструктору: {ADMIN_TELEGRAM}»

Отвечайте кратко (до 250 слов), только на русском."""
        },
        {
            "role": "user",
            "content": "Составь безопасный комплекс цигун для реабилитации с учётом всех ограничений подвижности."
        },
    ]
    
    try:
        ai_reply = generate_with_fallback(messages, max_tokens=450, temperature=0.5, top_p=0.9)
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
            reply_markup=get_main_menu_keyboard(),
        )
        logger.error(f"Ошибка генерации: {e}")
        return ConversationHandler.END

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "🧘 Новый комплекс (новый опрос)":
        return await start(update, context)
    elif text == "📋 Получить полный комплекс":
        # Переадресация на приложение RehabFlow
        app_url = "https://rehabflow1.vercel.app/"
        await update.message.reply_text(
            f"🌿 Для получения полного персонального комплекса упражнений (10-15 упражнений) "
            f"пройдите опрос в нашем приложении:\n\n"
            f"🔗 {app_url}\n\n"
            f"✅ Комплекс будет отправлен вам на email и инструктору!",
            reply_markup=get_main_menu_keyboard(),
        )
        return
    elif text == "👤 Мой профиль":
        user_id = str(update.effective_user.id)
        profiles = load_profiles()
        profile = profiles.get(user_id, {})
        if not profile.get("completed"):
            await update.message.reply_text("Сначала пройдите опрос командой /start", reply_markup=get_main_menu_keyboard())
            return
        diagnoses_text = "\n".join([f" • {d['type']}: {d['timing']}" for d in profile.get("diagnoses_details", [])]) or " не указаны"
        mobility_ru = {
            "лежачий": "🛏️ Лежачий",
            "сидячий": "🪑 Сидячий",
            "стоячий_с_опорой": "🪑➡️ Стоячий с опорой",
            "полноценная": "🚶 Полноценная подвижность",
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
            reply_markup=get_main_menu_keyboard(),
        )
        return
    await update.message.reply_text(
        "Для получения комплекса упражнений начните опрос командой /start",
        reply_markup=get_main_menu_keyboard(),
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
        "feedback_bad": "ухудшилось",
    }
    feedback_type = feedback_map.get(query.data, "неизвестно")
    if "feedback_history" not in profile:
        profile["feedback_history"] = []
    profile["feedback_history"].append({
        "date": datetime.now().isoformat(),
        "type": feedback_type,
        "days_since_registration": (datetime.now() - datetime.fromisoformat(profile["registered_at"].replace("Z", "+00:00"))).days,
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

# === ЗАПУСК БОТА ===
def main():
    logger.info("="*70)
    logger.info("🌿 ЦИГУН-РЕАБИЛИТАЦИЯ: запуск бота через вебхуки")
    logger.info("✅ Работает как бесплатный Web Service на Render.com")
    logger.info("="*70)
    # Создаём приложение
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
    # === НАСТРОЙКИ ВЕБХУКА ДЛЯ RENDER ===
    port = int(os.environ.get("PORT", 10000))
    render_hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "localhost")
    webhook_url = f"https://{render_hostname}/{TELEGRAM_TOKEN}"
    logger.info(f"🌐 Webhook URL: {webhook_url}")
    logger.info(f"🚪 Порт: {port}")
    logger.info("\n✅ Бот запускается через встроенный вебхук (без Flask)...\n")
    # Запускаем встроенный веб-сервер
    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        webhook_url=webhook_url,
        url_path=TELEGRAM_TOKEN,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
