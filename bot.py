import openai
import json
import csv
import asyncio
from datetime import datetime
from pathlib import Path
from io import StringIO
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

# === ВАШИ ДАННЫЕ (ОБЯЗАТЕЛЬНО ЗАМЕНИТЬ!) ===
TELEGRAM_TOKEN = "8536802808:AAGrOp-tWeIhB_kUJ2wXz5magPG5TxyepNE"  # ← Получите у @BotFather
IO_NET_API_KEY = "io-v2-eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJvd25lciI6IjYxZWU2OTJiLWE1NWQtNDZiMC04ODk3LWFiYWY5ZGU1YmQxOSIsImV4cCI6NDkyMzc5MTI1M30.UDP8-NIzExPlq9T8QYVVlSI1b9lD-BvejY_D4cRORH3-SAH7tUzJqK6SsMqF0ZVH-MIsSC9wew-s5gfUf4UYiw"  # ← Очистите через Блокнот!
ADMIN_TELEGRAM = "@cigunrehab"  # ← Ваш публичный юзернейм для клиентов
ADMIN_CHAT_ID = 6810836580  # ← ВАШ ЛИЧНЫЙ TELEGRAM ID (узнайте у @userinfobot)

openai.api_key = IO_NET_API_KEY
openai.api_base = "https://api.intelligence.io.solutions/api/v1"  # БЕЗ ПРОБЕЛОВ!

# === ХРАНИЛИЩЕ ПРОФИЛЕЙ ===
DATA_FILE = Path("users_data.json")

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
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(profiles, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ Ошибка сохранения профилей: {e}")

# === СИСТЕМНЫЙ ПРОМПТ С ПРАВИЛАМИ БЕЗОПАСНОСТИ ===
SYSTEM_PROMPT = """Вы — инструктор по цигун для реабилитации. Составляете БЕЗОПАСНЫЕ комплексы с учётом ограничений подвижности.

ПРОФИЛЬ ПАЦИЕНТА:
{profile_info}

КРИТИЧЕСКИЕ ПРАВИЛА БЕЗОПАСНОСТИ (НАРУШЕНИЕ = ОПАСНОСТЬ!):
1. ЕСЛИ ПАЦИЕНТ ЛЕЖАЧИЙ → ТОЛЬКО упражнения лёжа (дыхание, микродвижения пальцами, визуализация). НИКАКИХ сидячих/стоячих упражнений!
2. ЕСЛИ СИДЯЧИЙ → ТОЛЬКО сидячие упражнения (руки, дыхание, повороты корпуса с опорой). НЕ рекомендовать стоять!
3. ЕСЛИ СТОЯЧИЙ С ОПОРОЙ → короткие стоячие упражнения (макс. 1-2 мин) ТОЛЬКО с опорой на стул/стену. Избегать баланса без опоры!
4. Для инсульта/инфаркта: избегать резких движений, упор на дыхание и расслабление. Максимальная длительность сессии — 10 минут.
5. Для травм: избегать нагрузки на повреждённую область. Предлагать альтернативные движения.

СТРУКТУРА КОМПЛЕКСА (обязательно):
• Название упражнения
• Положение тела (чётко: лёжа/сидя/стоя с опорой)
• Дыхание (ритм: вдох/выдох в секундах)
• Движения (амплитуда, скорость)
• Длительность и повторения
• Цель упражнения

ОБЯЗАТЕЛЬНО В КОНЦЕ КАЖДОГО ОТВЕТА:
«❗ Обязательно проконсультируйтесь с лечащим врачом перед началом практики.
Для детального персонализированного комплекса напишите инструктору: {admin_contact}»

Отвечайте кратко (до 250 слов), только на русском языке. НИКОГДА не ставьте диагноз."""

# === КЛАВИАТУРЫ ===
def get_diagnosis_selection_keyboard(selected=None):
    """Клавиатура с мультивыбором диагнозов"""
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
    """Начало опроса — всегда с чистого листа для нового комплекса"""
    # Очищаем предыдущие данные для нового опроса
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
    
    # Если нажата кнопка завершения
    if text == "✅ Выбрал(а) всё":
        if not context.user_data["profile"]["diagnoses"]:
            await update.message.reply_text(
                "⚠️ Выберите хотя бы один диагноз:",
                reply_markup=get_diagnosis_selection_keyboard()
            )
            return ASK_DIAGNOSES_SELECTION
        
        # Сохраняем выбранные диагнозы и начинаем уточнение давности
        context.user_data["diagnosis_index"] = 0
        return await ask_diagnosis_timing(update, context)
    
    # Добавляем/удаляем диагноз из списка
    diagnosis = diagnosis_map.get(text)
    if diagnosis:
        diagnoses_list = context.user_data["profile"]["diagnoses"]
        if diagnosis in diagnoses_list:
            diagnoses_list.remove(diagnosis)
        else:
            diagnoses_list.append(diagnosis)
        
        # Показываем текущий выбор
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
    """Уточняем давность для КАЖДОГО выбранного диагноза"""
    diagnoses = context.user_data["profile"]["diagnoses"]
    idx = context.user_data.get("diagnosis_index", 0)
    
    # Если все диагнозы обработаны — переходим к подвижности
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
    """Сохраняем давность для текущего диагноза и переходим к следующему"""
    timing = update.message.text.strip()
    diagnosis = context.user_data["current_diagnosis"]
    
    # Инициализируем список диагнозов с деталями
    if "diagnoses_details" not in context.user_data["profile"]:
        context.user_data["profile"]["diagnoses_details"] = []
    
    context.user_data["profile"]["diagnoses_details"].append({
        "type": diagnosis,
        "timing": timing
    })
    
    # Переходим к следующему диагнозу
    context.user_data["diagnosis_index"] = context.user_data.get("diagnosis_index", 0) + 1
    return await ask_diagnosis_timing(update, context)

async def ask_mobility(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вопрос о подвижности — КРИТИЧЕСКИ ВАЖНО ДЛЯ БЕЗОПАСНОСТИ"""
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
    """Сохраняем самочувствие, уведомляем админа и генерируем комплекс"""
    context.user_data["profile"]["wellbeing"] = update.message.text.strip()
    context.user_data["profile"]["completed"] = True
    context.user_data["profile"]["registered_at"] = update.message.date.isoformat()
    context.user_data["profile"]["next_reminder_days"] = [3, 7, 14]  # Дни для напоминаний
    context.user_data["profile"]["last_reminder_sent"] = None
    
    # Сохраняем профиль в файл
    user_id = str(update.effective_user.id)
    profiles = load_profiles()
    
    # Проверяем, новый ли клиент
    is_new_client = user_id not in profiles
    
    profiles[user_id] = context.user_data["profile"]
    save_profiles(profiles)
    
    # === УВЕДОМЛЕНИЕ АДМИНИСТРАТОРА О НОВОМ КЛИЕНТЕ ===
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
            
            # Отправляем админу
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=admin_message
            )
            print(f"✅ Уведомление админу отправлено о новом клиенте {user_id}")
        except Exception as e:
            print(f"⚠️ Не удалось отправить уведомление админу: {e}")
    
    # Генерируем комплекс для пользователя
    await update.message.reply_text(
        "✅ Опрос завершён! Анализирую данные и составляю БЕЗОПАСНЫЙ комплекс упражнений...",
        reply_markup=ReplyKeyboardRemove()
    )
    return await generate_complex(update, context)

async def generate_complex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Генерация комплекса с учётом ВСЕХ данных пациента"""
    profile = context.user_data.get("profile", {})
    
    # Формируем подробное описание профиля для ИИ
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
    
    # Отправляем "Думаю..."
    thinking_msg = await update.message.reply_text("Практикую осознанность... 🧘‍♂️")
    
    try:
        response = openai.ChatCompletion.create(
            model="moonshotai/Kimi-K2-Instruct-0905",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT.format(
                    profile_info=profile_info,
                    admin_contact=ADMIN_TELEGRAM
                )},
                {"role": "user", "content": "Составь безопасный комплекс цигун для реабилитации с учётом всех ограничений подвижности."}
            ],
            max_tokens=450,
            temperature=0.5,
            top_p=0.9,
        )
        
        ai_reply = response.choices[0].message.content.strip()
        
        # Удаляем "Думаю..." и отправляем ответ
        try:
            await thinking_msg.delete()
        except:
            pass
        
        # Добавляем обязательные предупреждения, если ИИ их пропустил
        if "врач" not in ai_reply.lower() and "консульт" not in ai_reply.lower():
            ai_reply += "\n\n❗ Обязательно проконсультируйтесь с лечащим врачом перед практикой."
        
        if ADMIN_TELEGRAM not in ai_reply:
            ai_reply += f"\n\nДля детального персонализированного комплекса напишите инструктору: {ADMIN_TELEGRAM}"
        
        await update.message.reply_text(ai_reply, reply_markup=get_main_menu_keyboard())
        return ConversationHandler.END
        
    except Exception as e:
        try:
            await thinking_msg.delete()
        except:
            pass
        
        await update.message.reply_text(
            f"😔 Не удалось составить комплекс. Это может быть связано с нагрузкой на сервер.\n"
            f"Попробуйте через минуту или напишите напрямую инструктору: {ADMIN_TELEGRAM}",
            reply_markup=get_main_menu_keyboard()
        )
        print(f"Ошибка генерации комплекса: {e}")
        return ConversationHandler.END

# === ОБРАБОТКА КНОПОК МЕНЮ ===
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    if text == "🧘 Новый комплекс (новый опрос)":
        # Полностью новый опрос — очищаем контекст
        return await start(update, context)
    
    elif text == "👤 Мой профиль":
        await show_profile(update, context)
        return
    
    elif text == "👨‍🏫 К инструктору":
        await update.message.reply_text(
            f"👨‍🏫 Для глубокой персонализации и сопровождения в реабилитации напишите инструктору:\n\n"
            f"{ADMIN_TELEGRAM}\n\n"
            "Он составит детальный план с учётом всех нюансов вашего состояния и будет корректировать его по мере прогресса.",
            reply_markup=get_main_menu_keyboard()
        )
        return
    
    # Если пользователь пишет вне контекста опроса — предлагаем начать заново
    await update.message.reply_text(
        "Для получения комплекса упражнений начните опрос командой /start или кнопкой «🧘 Новый комплекс»",
        reply_markup=get_main_menu_keyboard()
    )

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    profiles = load_profiles()
    profile = profiles.get(user_id, {})
    
    if not profile.get("completed"):
        await update.message.reply_text(
            "Сначала пройдите опрос командой /start",
            reply_markup=get_main_menu_keyboard()
        )
        return
    
    # Формируем читаемый профиль
    diagnoses_text = "\n".join([
        f"  • {d['type']}: {d['timing']}" 
        for d in profile.get("diagnoses_details", [])
    ]) or "  не указаны"
    
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

# === НАПОМИНАНИЯ КЛИЕНТАМ (безопасная реализация) ===
async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача для отправки напоминаний клиентам"""
    profiles = load_profiles()
    today = datetime.now()
    updated_profiles = False
    
    for user_id, profile in profiles.items():
        if not profile.get("completed") or not profile.get("next_reminder_days"):
            continue
        
        # Рассчитываем дни с регистрации
        reg_date = datetime.fromisoformat(profile["registered_at"].replace("Z", "+00:00"))
        days_since_reg = (today - reg_date).days
        
        # Проверяем, нужно ли отправить напоминание сегодня
        reminders_due = [d for d in profile["next_reminder_days"] if d == days_since_reg]
        
        if reminders_due:
            try:
                # Формируем сообщение напоминания
                reminder_text = (
                    f"🌿 Привет, {profile.get('name', 'друг')}!\n\n"
                    f"Прошло {days_since_reg} дней с вашего первого комплекса цигун.\n"
                    f"Как ваше самочувствие улучшилось?\n\n"
                    "Ваши практики приносят результаты:\n"
                    "✓ Улучшение кровообращения\n"
                    "✓ Снижение стресса и тревожности\n"
                    "✓ Повышение подвижности суставов\n"
                    "✓ Восстановление координации движений\n\n"
                    "Хотите, чтобы я составил для вас РАСШИРЕННУЮ программу "
                    "с учётом ваших текущих ощущений и прогресса?"
                )
                
                # Отправляем сообщение с кнопками обратной связи
                await context.bot.send_message(
                    chat_id=int(user_id),
                    text=reminder_text,
                    reply_markup=get_feedback_keyboard()
                )
                
                # Обновляем данные профиля: удаляем отправленное напоминание
                profile["next_reminder_days"] = [d for d in profile["next_reminder_days"] if d not in reminders_due]
                profile["last_reminder_sent"] = today.isoformat()
                updated_profiles = True
                
                print(f"✅ Напоминание отправлено клиенту {user_id} ({days_since_reg} дней)")
                
            except Exception as e:
                # Если пользователь заблокировал бота — пропускаем
                if "blocked" in str(e).lower() or "not found" in str(e).lower():
                    print(f"⚠️ Клиент {user_id} заблокировал бота — пропускаем")
                else:
                    print(f"⚠️ Ошибка отправки напоминания {user_id}: {e}")
    
    if updated_profiles:
        save_profiles(profiles)

async def handle_feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатия кнопок обратной связи"""
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
    
    # Сохраняем обратную связь
    if "feedback_history" not in profile:
        profile["feedback_history"] = []
    
    profile["feedback_history"].append({
        "date": datetime.now().isoformat(),
        "type": feedback_type,
        "days_since_registration": (datetime.now() - datetime.fromisoformat(profile["registered_at"].replace("Z", "+00:00"))).days
    })
    
    profiles[user_id] = profile
    save_profiles(profiles)
    
    # Формируем ответ в зависимости от типа обратной связи
    if query.data == "feedback_good":
        response_text = (
            "🌟 Отлично! Рад, что практики приносят пользу!\n\n"
            "Для максимального эффекта предлагаю персонализированную программу "
            "с учётом вашего прогресса. Мой инструктор составит её специально для вас:\n\n"
            f"👉 Напишите {ADMIN_TELEGRAM}"
        )
    elif query.data == "feedback_neutral":
        response_text = (
            "🧘 Понимаю, изменения требуют времени. Главное — регулярность!\n\n"
            "Через 2-3 недели систематических практик вы заметите улучшения.\n"
            "Хотите, чтобы инструктор подобрал упражнения под ваш текущий уровень?\n\n"
            f"👉 Напишите {ADMIN_TELEGRAM}"
        )
    elif query.data == "feedback_bad":
        response_text = (
            "😔 Сочувствую. Важно помнить: цигун — дополнение к лечению, а не замена.\n\n"
            "❗ Настоятельно рекомендую проконсультироваться с врачом.\n"
            "Инструктор поможет адаптировать практики под ваше состояние:\n\n"
            f"👉 Напишите {ADMIN_TELEGRAM}"
        )
    else:  # feedback_details
        response_text = (
            "💬 Расскажите подробнее о своих ощущениях в ответном сообщении.\n"
            "Инструктор изучит вашу ситуацию и предложит оптимальную программу:\n\n"
            f"👉 {ADMIN_TELEGRAM}"
        )
    
    await query.edit_message_text(
        text=response_text,
        reply_markup=get_main_menu_keyboard()
    )

# === КОМАНДЫ ДЛЯ АДМИНИСТРАТОРА ===
async def show_new_clients(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать новых клиентов (не просмотренных админом)"""
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Эта команда доступна только администратору.")
        return
    
    profiles = load_profiles()
    new_clients = [
        (uid, p) for uid, p in profiles.items() 
        if p.get("completed") and not p.get("viewed_by_admin", False)
    ]
    
    if not new_clients:
        await update.message.reply_text("✅ Нет новых клиентов. Все профили просмотрены.")
        return
    
    # Формируем таблицу
    table = "🆕 НОВЫЕ КЛИЕНТЫ (ещё не просмотрены):\n\n"
    table += f"{'ID':<12} {'Имя':<15} {'Возр.':<6} {'Подвижность':<15} {'Диагнозы':<30}\n"
    table += "="*80 + "\n"
    
    for user_id, profile in new_clients[:10]:  # Первые 10 для удобства
        mobility_short = {
            "лежачий": "лежачий",
            "сидячий": "сидячий",
            "стоячий_с_опорой": "с опорой",
            "полноценная": "полная"
        }.get(profile.get("mobility"), profile.get("mobility", "-"))
        
        diagnoses = ", ".join([d["type"] for d in profile.get("diagnoses_details", [])[:2]]) or "-"
        if len(diagnoses) > 25:
            diagnoses = diagnoses[:22] + "..."
        
        table += f"{user_id:<12} {profile.get('name', '-'):<15} {profile.get('age', '-'):>5}  {mobility_short:<15} {diagnoses:<30}\n"
    
    if len(new_clients) > 10:
        table += f"\n... и ещё {len(new_clients) - 10} клиентов. Используйте /export для полного списка."
    
    table += "\n\n✅ Чтобы отметить как просмотренных, используйте /mark_viewed"
    
    await update.message.reply_text(f"<pre>{table}</pre>", parse_mode="HTML")

async def mark_viewed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отметить всех новых клиентов как просмотренных"""
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Эта команда доступна только администратору.")
        return
    
    profiles = load_profiles()
    count = 0
    for uid, profile in profiles.items():
        if profile.get("completed") and not profile.get("viewed_by_admin", False):
            profile["viewed_by_admin"] = True
            count += 1
    
    save_profiles(profiles)
    
    if count > 0:
        await update.message.reply_text(f"✅ Отмечено {count} клиентов как просмотренных.")
    else:
        await update.message.reply_text("ℹ️ Нет новых клиентов для отметки.")

async def show_all_clients(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать всех клиентов (с пагинацией)"""
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Эта команда доступна только администратору.")
        return
    
    profiles = load_profiles()
    all_clients = [
        (uid, p) for uid, p in profiles.items() if p.get("completed")
    ]
    
    if not all_clients:
        await update.message.reply_text("📭 Нет зарегистрированных клиентов.")
        return
    
    # Сортируем по дате регистрации (новые сверху)
    all_clients.sort(
        key=lambda x: x[1].get("registered_at", "2000-01-01"), 
        reverse=True
    )
    
    # Пагинация: 10 клиентов на страницу
    page = int(context.args[0]) if context.args else 1
    page_size = 10
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    paginated = all_clients[start_idx:end_idx]
    
    # Формируем таблицу
    table = f"👥 ВСЕ КЛИЕНТЫ (страница {page}/{(len(all_clients) + page_size - 1) // page_size}):\n\n"
    table += f"{'ID':<12} {'Имя':<12} {'Возр.':<6} {'Подв.':<10} {'Диагнозы':<25} {'Дата':<12}\n"
    table += "="*85 + "\n"
    
    for user_id, profile in paginated:
        mobility_short = {
            "лежачий": "леж",
            "сидячий": "сид",
            "стоячий_с_опорой": "опора",
            "полноценная": "полн"
        }.get(profile.get("mobility"), "-")
        
        diagnoses = ", ".join([d["type"] for d in profile.get("diagnoses_details", [])[:2]]) or "-"
        if len(diagnoses) > 22:
            diagnoses = diagnoses[:19] + "..."
        
        reg_date = profile.get("registered_at", "")[:10] if profile.get("registered_at") else "-"
        
        table += f"{user_id:<12} {profile.get('name', '-'):<12} {profile.get('age', '-'):>5}  {mobility_short:<10} {diagnoses:<25} {reg_date:<12}\n"
    
    # Кнопки навигации
    nav = []
    if page > 1:
        nav.append(f"/clients {page-1} ← Предыдущая")
    if end_idx < len(all_clients):
        nav.append(f"Следующая → /clients {page+1}")
    
    if nav:
        table += "\n" + " | ".join(nav)
    
    table += f"\n\nВсего клиентов: {len(all_clients)}"
    
    await update.message.reply_text(f"<pre>{table}</pre>", parse_mode="HTML")

async def export_clients(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экспорт данных в CSV (отправка файла)"""
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Эта команда доступна только администратору.")
        return
    
    profiles = load_profiles()
    clients = [(uid, p) for uid, p in profiles.items() if p.get("completed")]
    
    if not clients:
        await update.message.reply_text("📭 Нет данных для экспорта.")
        return
    
    # Создаём CSV в памяти
    output = StringIO()
    writer = csv.writer(output)
    
    # Заголовки
    writer.writerow([
        "Telegram_ID", "Имя", "Возраст", "Рост_см", "Вес_кг", 
        "Диагноз_1", "Давность_1", "Диагноз_2", "Давность_2", "Диагноз_3", "Давность_3",
        "Подвижность", "Самочувствие", "Дата_регистрации", "Обратная_связь"
    ])
    
    # Данные
    for user_id, profile in clients:
        diagnoses = profile.get("diagnoses_details", [])
        feedback_summary = "; ".join([
            f"{f['type']}({f['days_since_registration']}дн)" 
            for f in profile.get("feedback_history", [])[:3]
        ]) or "нет"
        
        row = [
            user_id,
            profile.get("name", ""),
            profile.get("age", ""),
            profile.get("height", ""),
            profile.get("weight", ""),
            diagnoses[0]["type"] if len(diagnoses) > 0 else "",
            diagnoses[0]["timing"] if len(diagnoses) > 0 else "",
            diagnoses[1]["type"] if len(diagnoses) > 1 else "",
            diagnoses[1]["timing"] if len(diagnoses) > 1 else "",
            diagnoses[2]["type"] if len(diagnoses) > 2 else "",
            diagnoses[2]["timing"] if len(diagnoses) > 2 else "",
            profile.get("mobility", ""),
            profile.get("wellbeing", "").replace("\n", " ").replace("\r", "")[:200],
            profile.get("registered_at", ""),
            feedback_summary
        ]
        writer.writerow(row)
    
    # Отправляем файл
    output.seek(0)
    await update.message.reply_document(
        document=output.getvalue().encode("utf-8-sig"),  # utf-8-sig для корректной кириллицы в Excel
        filename=f"clients_export_{update.message.date.strftime('%Y%m%d_%H%M')}.csv",
        caption=f"✅ Экспортировано {len(clients)} клиентов"
    )
    print(f"✅ Экспорт клиентов выполнен администратором {update.effective_user.id}")

async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало рассылки — запрос текста объявления"""
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Эта команда доступна только администратору.")
        return
    
    await update.message.reply_text(
        "📣 Режим рассылки включён.\n\n"
        "Введите текст объявления для ВСЕХ клиентов:\n"
        "(Отправьте /cancel для отмены)"
    )
    return BROADCAST_WAITING

async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправка рассылки всем клиентам"""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return ConversationHandler.END
    
    broadcast_text = update.message.text.strip()
    
    if broadcast_text.lower() == "/cancel":
        await update.message.reply_text("❌ Рассылка отменена.", reply_markup=get_main_menu_keyboard())
        return ConversationHandler.END
    
    profiles = load_profiles()
    clients = [uid for uid, p in profiles.items() if p.get("completed")]
    
    if not clients:
        await update.message.reply_text("📭 Нет клиентов для рассылки.")
        return ConversationHandler.END
    
    # Отправляем рассылку
    success = 0
    failed = 0
    
    progress_msg = await update.message.reply_text(f"📤 Отправляю {len(clients)} клиентам...")
    
    for i, user_id in enumerate(clients, 1):
        try:
            await context.bot.send_message(
                chat_id=int(user_id),
                text=f"📢 ОБЪЯВЛЕНИЕ ОТ ИНСТРУКТОРА:\n\n{broadcast_text}",
                reply_markup=get_main_menu_keyboard()
            )
            success += 1
            
            # Обновляем прогресс каждые 5 клиентов
            if i % 5 == 0 or i == len(clients):
                await progress_msg.edit_text(
                    f"📤 Отправлено {i}/{len(clients)} клиентам...\n"
                    f"✅ Успешно: {success} | ❌ Ошибок: {failed}"
                )
                
            # Небольшая задержка для избежания лимитов Telegram API
            await asyncio.sleep(0.3)
            
        except Exception as e:
            failed += 1
            print(f"⚠️ Ошибка отправки {user_id}: {e}")
    
    # Итоговый отчёт
    await progress_msg.edit_text(
        f"✅ Рассылка завершена!\n\n"
        f"Всего клиентов: {len(clients)}\n"
        f"Успешно доставлено: {success}\n"
        f"Ошибок: {failed}\n\n"
        f"Текст объявления:\n{broadcast_text[:100]}..."
    )
    
    return ConversationHandler.END

async def start_personal_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало отправки личного сообщения клиенту"""
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Эта команда доступна только администратору.")
        return
    
    await update.message.reply_text(
        "✍️ Отправка личного сообщения клиенту.\n\n"
        "Введите ID клиента и текст сообщения через пробел:\n"
        "Пример: `123456789 Здравствуйте, Анна! Как ваше самочувствие?`\n"
        "Или используйте команду: /message 123456789 Текст сообщения"
    )
    return MESSAGE_WAITING

async def send_personal_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправка личного сообщения клиенту"""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return ConversationHandler.END
    
    # Обрабатываем как команду /message, так и обычный текст
    if update.message.text.startswith("/message"):
        parts = update.message.text.split(maxsplit=2)
        if len(parts) < 3:
            await update.message.reply_text("❌ Неверный формат. Пример:\n/message 123456789 Привет!")
            return ConversationHandler.END
        user_id = parts[1]
        message_text = parts[2]
    else:
        parts = update.message.text.split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("❌ Неверный формат. Пример:\n123456789 Привет!")
            return ConversationHandler.END
        user_id = parts[0]
        message_text = parts[1]
    
    # Валидация ID
    if not user_id.isdigit():
        await update.message.reply_text("❌ ID должен быть числом. Пример: 123456789")
        return ConversationHandler.END
    
    # Проверяем наличие клиента в базе
    profiles = load_profiles()
    if user_id not in profiles or not profiles[user_id].get("completed"):
        await update.message.reply_text(
            f"❌ Клиент с ID {user_id} не найден в базе.\n"
            "Проверьте ID в таблице: /clients"
        )
        return ConversationHandler.END
    
    # Отправляем сообщение
    try:
        await context.bot.send_message(
            chat_id=int(user_id),
            text=f"👤 ЛИЧНОЕ СООБЩЕНИЕ ОТ ИНСТРУКТОРА:\n\n{message_text}",
            reply_markup=get_main_menu_keyboard()
        )
        
        # Подтверждение админу
        profile = profiles[user_id]
        await update.message.reply_text(
            f"✅ Сообщение отправлено клиенту:\n"
            f"ID: {user_id}\n"
            f"Имя: {profile.get('name', '-')}\n"
            f"Возраст: {profile.get('age', '-')} лет\n\n"
            f"Текст:\n{message_text}"
        )
        
    except Exception as e:
        await update.message.reply_text(
            f"❌ Ошибка отправки клиенту {user_id}:\n{str(e)[:200]}"
        )
        print(f"Ошибка отправки личного сообщения: {e}")
    
    return ConversationHandler.END

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика по клиентам"""
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Эта команда доступна только администратору.")
        return
    
    profiles = load_profiles()
    all_clients = [p for p in profiles.values() if p.get("completed")]
    new_today = [
        p for p in all_clients 
        if datetime.fromisoformat(p["registered_at"].replace("Z", "+00:00")).date() == datetime.now().date()
    ]
    
    # Статистика по подвижности
    mobility_stats = {"лежачий": 0, "сидячий": 0, "стоячий_с_опорой": 0, "полноценная": 0}
    for p in all_clients:
        m = p.get("mobility")
        if m in mobility_stats:
            mobility_stats[m] += 1
    
    # Статистика по диагнозам
    diagnosis_counts = {}
    for p in all_clients:
        for d in p.get("diagnoses_details", []):
            diagnosis_counts[d["type"]] = diagnosis_counts.get(d["type"], 0) + 1
    
    stats_text = (
        f"📊 СТАТИСТИКА КЛИЕНТОВ\n\n"
        f"Всего клиентов: {len(all_clients)}\n"
        f"Новых сегодня: {len(new_today)}\n\n"
        f"По подвижности:\n"
        f"  🛏️ Лежачие: {mobility_stats['лежачий']}\n"
        f"  🪑 Сидячие: {mobility_stats['сидячий']}\n"
        f"  🪑➡️ С опорой: {mobility_stats['стоячий_с_опорой']}\n"
        f"  🚶 Полноценная: {mobility_stats['полноценная']}\n\n"
        f"Топ-3 диагнозов:\n"
    )
    
    top_diagnoses = sorted(diagnosis_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    for i, (diag, count) in enumerate(top_diagnoses, 1):
        stats_text += f"  {i}. {diag}: {count}\n"
    
    await update.message.reply_text(stats_text)

# === ЗАПУСК БОТА ===
def main():
    print("="*70)
    print("🌿 ЦИГУН-РЕАБИЛИТАЦИЯ: полная система управления практикой")
    print("✅ Рассылка объявлений (/broadcast)")
    print("✅ Личные сообщения клиентам (/message ID текст)")
    print("✅ Таблицы и экспорт данных (/clients, /export)")
    print("🔔 Уведомления о новых клиентах приходят администратору")
    print("💾 Данные сохраняются в users_data.json")
    print("="*70)
    
    # Создаём файл данных при первом запуске
    if not DATA_FILE.exists():
        save_profiles({})
        print(f"✅ Создан файл данных: {DATA_FILE}")
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # ConversationHandler для полного опросника
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
            BROADCAST_WAITING: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_broadcast)],
            MESSAGE_WAITING: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_personal_message)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
        allow_reentry=True,
    )
    
    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_feedback_callback))
    
    # Команды администратора
    application.add_handler(CommandHandler("new_clients", show_new_clients))
    application.add_handler(CommandHandler("mark_viewed", mark_viewed))
    application.add_handler(CommandHandler("clients", show_all_clients))
    application.add_handler(CommandHandler("export", export_clients))
    application.add_handler(CommandHandler("broadcast", start_broadcast))
    application.add_handler(CommandHandler("message", start_personal_message))
    application.add_handler(CommandHandler("stats", show_stats))
    
    # === НАСТРОЙКА НАПОМИНАНИЙ (безопасная) ===
    if application.job_queue is not None:
        job_queue = application.job_queue
        job_queue.run_daily(
            callback=send_reminder,
            time=datetime.strptime("10:00", "%H:%M").time(),
            name="daily_reminders"
        )
        print("⏰ Напоминания настроены: проверка каждый день в 10:00")
    else:
        print("⚠️  Напоминания отключены (требуется установка дополнительных зависимостей)")
        print("   Чтобы включить напоминания, выполните:")
        print("   pip install \"python-telegram-bot[job-queue]\"")
        print("   И перезапустите бота")
    
    print(f"\n✅ Бот запущен!")
    print(f"👤 Ваш публичный контакт для клиентов: {ADMIN_TELEGRAM}")
    print(f"🆔 Ваш личный Telegram ID для уведомлений: {ADMIN_CHAT_ID}")
    print("\n🛠️  КОМАНДЫ АДМИНИСТРАТОРА (только в вашем личном чате с ботом):")
    print("   /new_clients   - новые клиенты")
    print("   /clients        - все клиенты (с пагинацией)")
    print("   /export         - экспорт в CSV")
    print("   /broadcast      - рассылка объявления ВСЕМ")
    print("   /message ID текст - личное сообщение клиенту")
    print("   /stats          - статистика по клиентам")
    print("\n❗ ВАЖНО: замените значения TELEGRAM_TOKEN, IO_NET_API_KEY и ADMIN_CHAT_ID в начале кода!")
    print("Нажмите Ctrl+C для остановки.\n")
    
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()