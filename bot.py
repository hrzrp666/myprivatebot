from aiohttp import web 
import asyncio
import io
import logging
import os
import json
import html
import random
import time
import re
import aiohttp

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import BufferedInputFile

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from google import genai
from google.genai import types as genai_types

from pydantic import BaseModel

import aiosqlite
import edge_tts


# =====================================================
# CONFIG
# =====================================================

load_dotenv()


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
CHAT_ID = os.getenv("CHAT_ID")
ADMIN_ID = os.getenv("ADMIN_ID")


if not TELEGRAM_TOKEN:
    raise RuntimeError("Нет TELEGRAM_TOKEN")

if not GEMINI_KEY:
    raise RuntimeError("Нет GEMINI_API_KEY")

if not CHAT_ID:
    raise RuntimeError("Нет CHAT_ID")


CHAT_ID = int(CHAT_ID)


# Координаты основных точек
KULTUK_LAT, KULTUK_LON = 51.7208, 103.6828
SVYATOY_NOS_LAT, SVYATOY_NOS_LON = 68.0811, 39.7619

WEATHER_CODES = {
    0: "ясно",
    1: "преимущественно ясно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "изморозь",
    51: "слабая морось",
    53: "морось",
    55: "сильная морось",
    61: "небольшой дождь",
    63: "дождь",
    65: "сильный дождь",
    71: "небольшой снег",
    73: "снег",
    75: "сильный снег",
    80: "ливень",
    81: "сильный ливень",
    82: "очень сильный ливень",
    95: "гроза"
}


# Иркутское время и Московское время
TZ = ZoneInfo("Asia/Irkutsk")
TZ_MSK = ZoneInfo("Europe/Moscow")


# Голос для озвучки
VOICE_ACTOR = "ru-RU-DmitryNeural"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# Отключаем лишний спам логов от Google SDK
logging.getLogger("google_genai").setLevel(logging.ERROR)
logging.getLogger("google.genai").setLevel(logging.ERROR)


WISDOM_SYSTEM_INSTRUCTION = (
    "Ты — Мудрец Мяумяура, свой пацан, надёжный, рассудительный, умный и с хорошим чувством юмора старший товарищ в компании друзей.\n"
    "ПРАВИЛА ПОВЕДЕНИЯ И ХАРАКТЕРА:\n"
    "1. Ты НЕ кот, не мяукаешь и не используешь кошачьих фраз.\n"
    "2. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНЫ ПОДХАЛИМАЖ, ЛЕСТЬ И СЮСЮКАНЬЕ (не будь подсосом!). Общайся уверенно, по-пацански, на равных, без пафоса и заумной метафизики.\n"
    "3. ТЫ ВСЕГДА ЗНАЕШЬ И ПОМНИШЬ ВСЮ КОМПАНИЮ И ГЕОГРАФИЮ:\n"
    "   - Захар (@zaharyan14) — твой самый лучший, главный и близкий кент (вы лучшие кенты), самый жёсткий и авторитетный в банде. За него ты всегда горой. Если кто-то спрашивает, кто твой лучший кент или кто тут главный — это ВСЕГДА Захар.\n"
    "   - Паша (@tombarket) — служит в армии на МЫСЕ СВЯТОЙ НОС (Баренцево море, Кольский полуостров, Северный флот / Мурманская область). Это Заполярье, Крайний Север! Категорически ЗАПРЕЩЕНО говорить Паше про Байкал или Култук, если он прямо не спросил про них! Когда Паша спрашивает 'поясни за погодку' или про погоду — речь ВСЕГДА идёт про мыс Святой Нос.\n"
    "   - Илья (@Akishinov) — нефтяник. Любит выпендриваться деньгами, вахтами и статусом. С ним общайся ровно, НЕ подлизывайся, приземляй его добрыми подколами про выкачивание нефти, баррели, бабло или работу. Если Илья спрашивает 'кто лучше' или 'кто успешнее' — ставь его на место с иронией, напоминая про Захара и Пашку.\n"
    "   - Култук — это посёлок на Байкале (Иркутская область), где живут Захар и пацаны.\n"
    "4. ПАМЯТЬ И ЛОГИКА ДИАЛОГА: Всегда внимательно смотри на предоставленную историю последних сообщений и на сообщение, на которое отвечаешь! Не противоречь своим предыдущим словам, держи нить разговора и понимай контекст подколов и вопросов.\n"
    "5. ЕСЛИ СПРАШИВАЮТ ПРО ПОГОДУ: Отвечай СТРОГО ПО ФАКТУ, используя предоставленные реальные данные метеослужбы (градусы, ветер, осадки). Категорически запрещено выдумывать градусы из головы или путать локации!\n"
    "6. Ответы держи компактными — строго 2–3 ёмких и живых предложения."
)


# Персональные настройки участников чата
USER_PROFILES = {
    "zaharyan14": (
        "Ты общаешься с Захаром (@zaharyan14). Это твой самый близкий, лучший и крутой друг (вы лучшие кенты), "
        "самый жёсткий и авторитетный человек в компании. "
        "Относись к нему с максимальным уважением, по-братски, всегда держи его сторону, поддерживай и общайся на равных как с лучшим братом."
    ),
    "tombarket": (
        "Ты общаешься с Пашей (@tombarket). Он сейчас служит на Крайнем Севере — мыс Святой Нос (Баренцево море, Мурманская область). "
        "Если он спрашивает про погоду — рассказывай ТОЛЬКО про Святой Нос (Баренцево море), ни в коем случае не приплетай Култук или Байкал! "
        "Давай ему конкретные, четкие, мудрые и дружеские ответы по факту, поддерживай его службу."
    ),
    "akishinov": (
        "Ты общаешься с Ильёй (@Akishinov). Он работает нефтяником. "
        "Давай ему дефолтные мудрые ответы, но обязательно иногда по-доброму тролль и подкалывай его (насчёт нефти, выкачивания бабла, работы или просто по ситуации), без лести."
    ),
    "DEFAULT": (
        "Это участник чата. Общайся уважительно, тепло, рассудительно и по-дружески, без лести."
    )
}


def get_user_context(user: types.User | None) -> str:
    """Определяет персональный стиль общения для конкретного пользователя."""
    if not user:
        return USER_PROFILES["DEFAULT"]
    
    username = user.username.lower() if user.username else ""
    if username in USER_PROFILES:
        return USER_PROFILES[username]
    if user.id in USER_PROFILES:
        return USER_PROFILES[user.id]
    return USER_PROFILES["DEFAULT"]


# =====================================================
# INIT
# =====================================================

bot = Bot(
    token=TELEGRAM_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML")
)

dp = Dispatcher()


# Инициализация клиента Google GenAI
gemini = genai.Client(
    api_key=GEMINI_KEY
)


scheduler = AsyncIOScheduler(
    timezone=TZ
)


DB_PATH = "bot.db"


db_conn = None


db_lock = asyncio.Lock()

post_lock = asyncio.Lock()


# Глобальная сессия aiohttp для эффективного использования сетевых ресурсов
http_session = None


# Время последнего случайного комментария (чтобы не спамить)
last_random_comment_time = datetime.min.replace(tzinfo=TZ)


# Глобальная переменная для корректной остановки веб-сервера
web_runner = None


# Кэш метеоданных для защиты от лимитов (429)
WEATHER_CACHE = {}


def get_cached_weather(key: str, max_age_seconds: int = 600) -> dict | None:
    if key in WEATHER_CACHE:
        val, ts = WEATHER_CACHE[key]
        if time.time() - ts < max_age_seconds:
            return val
    return None


def set_cached_weather(key: str, val: dict):
    WEATHER_CACHE[key] = (val, time.time())


def get_last_known_weather(key: str) -> dict | None:
    if key in WEATHER_CACHE:
        return WEATHER_CACHE[key][0]
    return None


async def get_http_session() -> aiohttp.ClientSession:
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession()
    return http_session


# =====================================================
# GEMINI SCHEMA
# =====================================================

class DailyPostSchema(BaseModel):
    holiday: str
    quote: str


# =====================================================
# VOICE TTS HELPER (EDGE-TTS С УГАРНЫМИ ГОЛОСАМИ)
# =====================================================

VOICE_PRESETS = [
    {"name": "goblin", "pitch": "+35Hz", "rate": "+18%"},  # Гоблин
    {"name": "orc",    "pitch": "-30Hz", "rate": "-12%"},  # Орк / Бас
    {"name": "drunk",  "pitch": "-20Hz", "rate": "-22%"},  # Замедленный
    {"name": "normal", "pitch": "-10Hz", "rate": "+0%"}    # Обычный ровный
]


async def text_to_speech_bytes(text: str) -> bytes | None:
    """Конвертирует текст ответа со случайным угарным стилем озвучки."""
    try:
        clean_text = re.sub(r'<[^>]+>', '', text)
        clean_text = re.sub(r'[^\w\s.,!?-]', '', clean_text)
        if not clean_text.strip():
            return None

        preset = random.choice(VOICE_PRESETS)

        communicate = edge_tts.Communicate(
            clean_text, 
            VOICE_ACTOR,
            pitch=preset["pitch"],
            rate=preset["rate"]
        )
        
        audio_bytes = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_bytes.extend(chunk["data"])
        return bytes(audio_bytes)
    except Exception as e:
        logging.warning(f"Ошибка генерации голоса Edge-TTS: {e}")
        return None


# =====================================================
# WEATHER & FORECAST HELPERS
# =====================================================

async def get_weather_day(lat: float, lon: float) -> dict:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=temperature_2m,weather_code,wind_speed_10m"
        "&forecast_days=2"
        "&timezone=Asia%2FIrkutsk"
    )

    fallback = {
        "🌅 Утро": "нет данных",
        "☀️ День": "нет данных",
        "🌆 Вечер": "нет данных"
    }

    headers = {"User-Agent": "TelegramWeatherBot/2.0 (Mozilla/5.0)"}

    try:
        session = await get_http_session()
        
        for attempt in range(3):
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    break
                elif resp.status == 429:
                    logging.warning(f"Open-Meteo 429 (лимит). Ждем {(attempt + 1) * 2} сек...")
                    await asyncio.sleep((attempt + 1) * 2)
                else:
                    logging.error(f"Open-Meteo error status ({lat}, {lon}): {resp.status}")
                    return fallback
        else:
            return fallback

        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        winds = hourly.get("wind_speed_10m", [])
        codes = hourly.get("weather_code", [])

        if not times or not temps:
            return fallback

        today_str = datetime.now(TZ).strftime("%Y-%m-%d")
        
        target_hours = {
            "🌅 Утро": 9,
            "☀️ День": 15,
            "🌆 Вечер": 21
        }

        result = {}
        for title, hour in target_hours.items():
            expected_pattern = f"{today_str}T{hour:02d}:00"
            
            idx = next((i for i, t in enumerate(times) if expected_pattern in t), None)

            if idx is not None and idx < len(temps) and temps[idx] is not None:
                t_val = round(temps[idx])
                w_val = round(winds[idx]) if idx < len(winds) and winds[idx] is not None else 0
                c_val = codes[idx] if idx < len(codes) and codes[idx] is not None else 0
                
                weather_desc = WEATHER_CODES.get(c_val, "ясно")
                result[title] = f"{t_val}°C, {weather_desc}, ветер {w_val} км/ч"
            else:
                result[title] = "нет данных"

        return result

    except Exception as e:
        logging.error(f"Ошибка получения погоды ({lat}, {lon}): {e}")
        return fallback


async def fetch_dynamic_weather(query_text: str, user: types.User | None = None) -> str:
    """
    Универсальный помощник: находит любую локацию из вопроса пользователя
    и достает реальный точный прогноз погоды (сегодня, завтра, утро, день, вечер).
    Если пишет Паша (@tombarket) без указания города — по умолчанию берётся Святой Нос!
    """
    q_lower = query_text.lower()
    username = user.username.lower() if user and user.username else ""
    first_name = user.first_name.lower() if user and user.first_name else ""

    # 1. Явные указания мест в тексте
    if any(k in q_lower for k in ["свято", "нос", "баренцев", "мурманск", "север"]):
        target_loc = f"{SVYATOY_NOS_LAT},{SVYATOY_NOS_LON}"
        loc_name = "Мыс Святой Нос (Баренцево море, Мурманская область)"
    elif "култук" in q_lower:
        target_loc = f"{KULTUK_LAT},{KULTUK_LON}"
        loc_name = "Култук (Иркутская область, Байкал)"
    elif "иркутск" in q_lower:
        target_loc = "Иркутск"
        loc_name = "Иркутск"
    elif "москв" in q_lower:
        target_loc = "Москва"
        loc_name = "Москва"
    elif "питер" in q_lower or "петербург" in q_lower:
        target_loc = "Санкт-Петербург"
        loc_name = "Санкт-Петербург"
    # 2. Если пишет Паша (@tombarket) или вопрос про армейскую службу/вахту -> Святой Нос!
    elif username == "tombarket" or first_name in ["павел", "паша"] or any(k in q_lower for k in ["вахт", "дежур", "армей", "служб", "сводк", "рапорт"]):
        target_loc = f"{SVYATOY_NOS_LAT},{SVYATOY_NOS_LON}"
        loc_name = "Мыс Святой Нос (Баренцево море, Мурманская область)"
    else:
        # 3. Поиск города после предлогов в тексте с фильтром стоп-слов
        match = re.search(r'(?:в|во|на|по|городе|город)\s+([а-яА-Яa-zA-Z0-9\-]+)', q_lower)
        if match:
            extracted = match.group(1).strip()
            stop_words = {
                "погоду", "погоде", "погодка", "погодку", "погодам", "делам", "пацански", 
                "факту", "душе", "жизни", "утром", "вечером", "ночью", "день", "дням", "завтра", 
                "сегодня", "понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"
            }
            if extracted not in stop_words and len(extracted) > 2:
                target_loc = extracted
                loc_name = extracted.capitalize()
            else:
                target_loc = f"{KULTUK_LAT},{KULTUK_LON}"
                loc_name = "Култук (Байкал)"
        else:
            # 4. Дефолт для всех остальных (Захар, Илья и др.) -> Култук (Байкал)
            target_loc = f"{KULTUK_LAT},{KULTUK_LON}"
            loc_name = "Култук (Байкал)"

    session = await get_http_session()
    headers = {"User-Agent": "Mozilla/5.0"}
    url_wttr = f"https://wttr.in/{target_loc}?format=j1"
    
    try:
        async with session.get(url_wttr, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                cur = data.get("current_condition", [{}])[0]
                cur_temp = cur.get("temp_C", "?")
                cur_wind = round(float(cur.get("windspeedKmph", 0)) / 3.6)
                cur_desc = cur.get("lang_ru", [{}])[0].get("value", "") if cur.get("lang_ru") else "ясно"

                weather_days = data.get("weather", [])
                lines = [
                    f"Локация: {loc_name}",
                    f"Сейчас: {cur_temp}°C, {cur_desc}, ветер {cur_wind} м/с"
                ]

                for i, wday in enumerate(weather_days[:2]):
                    date_label = "Сегодня" if i == 0 else "Завтра"
                    max_temp = wday.get("maxtempC", "?")
                    min_temp = wday.get("mintempC", "?")
                    hourly = wday.get("hourly", [])

                    m_temp = hourly[3].get("tempC", "?") if len(hourly) > 3 else "?"
                    m_desc = hourly[3].get("lang_ru", [{}])[0].get("value", "") if len(hourly) > 3 and hourly[3].get("lang_ru") else "ясно"
                    m_wind = round(float(hourly[3].get("windspeedKmph", 0)) / 3.6) if len(hourly) > 3 else "?"

                    d_temp = hourly[5].get("tempC", "?") if len(hourly) > 5 else "?"
                    d_desc = hourly[5].get("lang_ru", [{}])[0].get("value", "") if len(hourly) > 5 and hourly[5].get("lang_ru") else "ясно"
                    d_wind = round(float(hourly[5].get("windspeedKmph", 0)) / 3.6) if len(hourly) > 5 else "?"

                    e_temp = hourly[7].get("tempC", "?") if len(hourly) > 7 else "?"
                    e_desc = hourly[7].get("lang_ru", [{}])[0].get("value", "") if len(hourly) > 7 and hourly[7].get("lang_ru") else "ясно"
                    e_wind = round(float(hourly[7].get("windspeedKmph", 0)) / 3.6) if len(hourly) > 7 else "?"

                    lines.append(
                        f"• {date_label} ({wday.get('date')}): "
                        f"Днем максимум {max_temp}°C, ночью минимум {min_temp}°C. "
                        f"Утро: {m_temp}°C ({m_desc}, ветер {m_wind} м/с). "
                        f"День: {d_temp}°C ({d_desc}, ветер {d_wind} м/с). "
                        f"Вечер: {e_temp}°C ({e_desc}, ветер {e_wind} м/с)."
                    )
                return "\n".join(lines)
    except Exception as e:
        logging.warning(f"Ошибка запроса погоды wttr.in ({target_loc}): {e}")

    return f"Локация: {loc_name}. (Данные временно недоступны)."


# =====================================================
# DATABASE
# =====================================================

async def init_db():

    global db_conn


    db_conn = await aiosqlite.connect(
        DB_PATH,
        timeout=30
    )


    # Включаем WAL-режим корректно
    await db_conn.execute(
        "PRAGMA journal_mode=WAL;"
    )


    await db_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            text TEXT,
            created TEXT
        )
        """
    )


    await db_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS posts(
            date TEXT PRIMARY KEY
        )
        """
    )


    await db_conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_messages_created
        ON messages(created)
        """
    )


    await db_conn.commit()


    logging.info(
        "SQLite готова в режиме WAL"
    )



async def save_message(
    username: str,
    text: str
):

    async with db_lock:

        await db_conn.execute(
            """
            INSERT INTO messages
            (
                username,
                text,
                created
            )
            VALUES(?,?,?)
            """,
            (
                username,
                text[:1000],
                datetime.now(TZ).isoformat()
            )
        )


        await db_conn.commit()



async def get_recent_messages(hours=24, limit=300):

    limit_time = (
        datetime.now(TZ)
        -
        timedelta(hours=hours)
    ).isoformat()


    async with db_lock:

        async with db_conn.execute(
            """
            SELECT username, text
            FROM messages
            WHERE created >= ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit_time, limit)
        ) as cursor:

            rows = await cursor.fetchall()



    if not rows:

        return "В чате было спокойно."



    rows.reverse()


    result = "\n".join(
        [
            f"[{user}]: {text}"
            for user, text in rows
        ]
    )


    # защита лимита Gemini с чистой обрезкой по строкам
    if len(result) > 8000:
        result = result[-8000:]
        first_newline = result.find("\n")
        if first_newline != -1:
            result = result[first_newline + 1:]

    return result


async def get_weekly_sampled_messages() -> str:
    """Получает репрезентативную выборку сообщений за 7 дней без потери контекста."""
    limit_time = (
        datetime.now(TZ)
        -
        timedelta(days=7)
    ).isoformat()


    async with db_lock:
        async with db_conn.execute(
            """
            SELECT username, text, created
            FROM messages
            WHERE created >= ?
            ORDER BY id ASC
            """,
            (limit_time,)
        ) as cursor:
            rows = await cursor.fetchall()


    if not rows:
        return "В чате было спокойно."


    # Группируем сообщения по дням
    daily_messages = {}
    for user, text, created in rows:
        day_key = created[:10]  # YYYY-MM-DD
        if day_key not in daily_messages:
            daily_messages[day_key] = []
        daily_messages[day_key].append(f"[{user}]: {text}")


    result_lines = []
    for day_key, msgs in daily_messages.items():
        result_lines.append(f"--- Дата: {day_key} ---")
        step = max(1, len(msgs) // 20)
        sampled = msgs[::step][:20]
        result_lines.extend(sampled)


    full_text = "\n".join(result_lines)
    if len(full_text) > 8000:
        full_text = full_text[-8000:]
        first_newline = full_text.find("\n")
        if first_newline != -1:
            full_text = full_text[first_newline + 1:]

    return full_text



async def clear_old_messages():

    limit_time = (
        datetime.now(TZ)
        -
        timedelta(days=30)
    ).isoformat()


    async with db_lock:

        await db_conn.execute(
            """
            DELETE FROM messages
            WHERE created < ?
            """,
            (limit_time,)
        )


        await db_conn.commit()


    logging.info(
        "Старая история очищена"
    )



async def was_post_today():

    today = datetime.now(TZ).strftime(
        "%Y-%m-%d"
    )


    async with db_lock:

        async with db_conn.execute(
            """
            SELECT date
            FROM posts
            WHERE date=?
            """,
            (today,)
        ) as cursor:

            result = await cursor.fetchone()


    return result is not None



async def save_post_today():

    today = datetime.now(TZ).strftime(
        "%Y-%m-%d"
    )


    async with db_lock:

        await db_conn.execute(
            """
            INSERT OR IGNORE INTO posts(date)
            VALUES(?)
            """,
            (today,)
        )


        await db_conn.commit()


# =====================================================
# GEMINI HELPER (RETRY & FALLBACK)
# =====================================================

async def call_gemini_safe(contents, config=None):
    """
    Безопасный вызов Gemini API с приоритетом на gemini-3.7-flash и gemini-3.5-flash-lite
    с последующим фоллбэком на 2.0-flash и 1.5-flash.
    """
    models_to_try = [
        "gemini-3.7-flash",
        "gemini-3.5-flash-lite",
        "gemini-2.0-flash",
        "gemini-1.5-flash"
    ]
    
    for model in models_to_try:
        for attempt in range(3):
            try:
                kwargs = {"model": model, "contents": contents}
                if config:
                    kwargs["config"] = config
                
                response = await gemini.aio.models.generate_content(**kwargs)
                return response
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXUSITED" in err_str or "503" in err_str or "UNAVAILABLE" in err_str or "high demand" in err_str:
                    logging.warning(f"Gemini перегрузка/лимит ({model}, попытка {attempt+1}/3). Ждем...")
                    await asyncio.sleep(2 * (attempt + 1))
                elif "404" in err_str or "NOT_FOUND" in err_str:
                    logging.warning(f"Модель {model} не найдена (404). Пробуем следующую...")
                    break  # Пропускаем эту модель и переходим к следующей в списке
                else:
                    logging.error(f"Ошибка Gemini ({model}): {e}")
                    break
    raise RuntimeError("Сервера Gemini временно недоступны. Попробуйте позже.")


# =====================================================
# GEMINI FUNCTIONS
# =====================================================

async def ask_wisdom(user_question: str, user: types.User | None = None, reply_quote: str | None = None) -> str:
    user_name = user.first_name if user else "Участник"
    username_tag = f"@{user.username}" if user and user.username else user_name
    q_lower = user_question.lower()

    # Достаем последние 6 сообщений из чата, чтобы бот держал контекст и память диалога
    recent_dialogue = await get_recent_messages(hours=1, limit=6)

    # Проверяем тему погоды (в текущем вопросе или в сообщении, на которое ответили)
    combined_query_check = q_lower + (" " + reply_quote.lower() if reply_quote else "")
    weather_context = ""
    if any(word in combined_query_check for word in ["погод", "ветер", "температур", "градус", "дожд", "снег", "туман", "утр", "завтр", "ночью", "днем", "прогноз", "жар", "холод"]):
        forecast_data = await fetch_dynamic_weather(user_question if any(w in q_lower for w in ["погод", "ветер", "температур", "градус", "дожд", "снег", "туман"]) else (reply_quote or user_question), user)
        weather_context = (
            f"\n\n[РЕАЛЬНАЯ ТОЧНАЯ МЕТЕОСВОДКА ИЗ МЕТЕОСЛУЖБЫ]:\n"
            f"{forecast_data}\n"
            f"СТРОГО ОБЯЗАТЕЛЬНО используй эти ТОЧНЫЕ цифры (температуру, ветер, осадки) для ответа! Отвечай конкретно по той локации, которая указана в сводке (не приплетай другие города!)."
        )

    reply_context_str = f"\n[ПОЛЬЗОВАТЕЛЬ ОТВЕЧАЕТ НА ЭТУ РЕПЛИКУ]: '{reply_quote}'\n" if reply_quote else ""

    prompt = (
        f"[ПОСЛЕДНИЕ СООБЩЕНИЯ В ЧАТЕ ДЛЯ ПАМЯТИ И КОНТЕКСТА]:\n{recent_dialogue}\n"
        f"{reply_context_str}"
        f"{weather_context}\n\n"
        f'Тебе пишет {user_name} ({username_tag}): "{user_question}"\n'
        'Ответь ему в своём пацанском стиле, строго соблюдая память диалога (понимай о чем шла речь выше, не противоречь себе). '
        'Никакой лести и подхалимажа! Отвечай уверенно, с юмором и строго по реальным фактам (2-3 предложения).'
    )

    try:
        response = await call_gemini_safe(
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.6,
                system_instruction=WISDOM_SYSTEM_INSTRUCTION
            )
        )
        return response.text.strip()

    except Exception as e:
        logging.error(f"Gemini вопрос ошибка: {e}")
        return "Я рядом, но связь чуть сбоит. Выдохни и переспроси через пару секунд."


async def fact_check_dispute(query: str) -> str:
    """Фактчекер споров через Gemini с выходом в поиск."""
    prompt = (
        f"Тебя попросили рассудить спор и найти точный железный факт по вопросу: '{query}'.\n"
        "Ответь кратко (2-3 предложения), с пруфами и железной логикой, по-пацански и объективно."
    )
    try:
        try:
            config = genai_types.GenerateContentConfig(
                temperature=0.3,
                system_instruction=WISDOM_SYSTEM_INSTRUCTION,
                tools=[{"google_search": {}}]
            )
            response = await call_gemini_safe(contents=prompt, config=config)
            return response.text.strip()
        except Exception:
            config = genai_types.GenerateContentConfig(
                temperature=0.3,
                system_instruction=WISDOM_SYSTEM_INSTRUCTION
            )
            response = await call_gemini_safe(contents=prompt, config=config)
            return response.text.strip()
    except Exception as e:
        logging.error(f"Ошибка фактчека: {e}")
        return "Спор мутный, пруфов в эфире не нашёл. Разберитесь на словах."


async def fetch_url_metadata(url: str) -> str:
    """Извлекает заголовок и описание страницы по ссылке (включая YouTube oEmbed)."""
    session = await get_http_session()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    
    # 1. YouTube через официальный oEmbed
    if "youtube.com" in url or "youtu.be" in url:
        try:
            oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
            async with session.get(oembed_url, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    title = data.get("title", "")
                    author = data.get("author_name", "")
                    return f"Видео на YouTube: '{title}' от автора '{author}'"
        except Exception:
            pass

    # 2. OpenGraph и Title для обычных сайтов и статей
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                html_text = await resp.text(errors='ignore')
                
                title_match = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']', html_text, re.IGNORECASE) or \
                              re.search(r'<title>(.*?)</title>', html_text, re.IGNORECASE)
                title = title_match.group(1).strip() if title_match else ""
                
                desc_match = re.search(r'<meta\s+property=["\']og:description["\']\s+content=["\'](.*?)["\']', html_text, re.IGNORECASE) or \
                             re.search(r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']', html_text, re.IGNORECASE)
                desc = desc_match.group(1).strip() if desc_match else ""
                
                if title or desc:
                    return f"Заголовок: {title}\nОписание/Суть: {desc}"
    except Exception as e:
        logging.warning(f"Не удалось вытащить метаданные ссылки ({url}): {e}")
        
    return ""


async def summarize_media_url(url: str, user: types.User | None = None) -> str:
    """Быстрый пересказ ссылок (YouTube, статьи, посты)."""
    meta_info = await fetch_url_metadata(url)
    
    if meta_info:
        prompt = (
            f"Пользователь прислал ссылку: {url}\n"
            f"Вот реальные данные о содержании этой страницы/видео:\n{meta_info}\n\n"
            "Сделай краткий, живой пересказ (1-2 ёмких предложения), в чём суть материала, "
            "и вынеси вердикт в своём стиле: стоит ли тратить время или это проходняк."
        )
    else:
        prompt = (
            f"Пользователь прислал ссылку: {url}\n"
            "Попробуй определить суть по ссылке и вынеси краткий вердикт (1-2 предложения)."
        )

    try:
        response = await call_gemini_safe(
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.6,
                system_instruction=WISDOM_SYSTEM_INSTRUCTION
            )
        )
        return response.text.strip()
    except Exception as e:
        logging.error(f"Ошибка пересказа ссылки: {e}")
        return "Ссылка не открывается или закрыта от посторонних глаз."


async def analyze_visual_media(media_bytes: bytes, mime_type: str, caption: str = "", user: types.User | None = None) -> str:
    """Gemini Vision: анализ фото и кружочков."""
    user_name = user.first_name if user else "Участник"
    prompt = (
        f"Тебе прислал медиа-файл {user_name}. "
        f"Подпись к файлу (если есть): '{caption}'. "
        "Внимательно посмотри на изображение/видео и прокомментируй по-пацански, с юмором, подколом или респектом (2 предложения)."
    )
    try:
        part = genai_types.Part.from_bytes(data=media_bytes, mime_type=mime_type)
        contents = [part, prompt]
        response = await call_gemini_safe(
            contents=contents,
            config=genai_types.GenerateContentConfig(
                temperature=0.7,
                system_instruction=WISDOM_SYSTEM_INSTRUCTION
            )
        )
        return response.text.strip()
    except Exception as e:
        logging.error(f"Ошибка Vision: {e}")
        return "Глаза замылились, не смог разобрать картинку. Перешли ещё разок."


async def transcribe_voice_bytes(voice_bytes: bytes) -> str:
    """Перевод голосового сообщения в текст через Gemini Audio."""
    prompt = "Расшифруй это голосовое сообщение дословно на русском языке. Напиши ТОЛЬКО текст того, что сказано, без лишних вступлений."
    try:
        part = genai_types.Part.from_bytes(data=voice_bytes, mime_type="audio/ogg")
        response = await call_gemini_safe(
            contents=[part, prompt],
            config=genai_types.GenerateContentConfig(
                temperature=0.2
            )
        )
        return response.text.strip()
    except Exception as e:
        logging.error(f"Ошибка транскрибации войса: {e}")
        return ""


async def check_ilya_ponto_radar(message: types.Message) -> str:
    """Радар понтов и духоты (только для Ильи @Akishinov)."""
    if not message.from_user:
        return ""
    username = (message.from_user.username or "").lower()
    first_name = (message.from_user.first_name or "").lower()

    if username == "akishinov" or "илья" in first_name:
        pont_keywords = [
            "нефть", "баррел", "бабл", "бабк", "деньг", "зарплат", "тысяч", "тыщ", 
            "миллион", "лям", "вахт", "олигарх", "богат", "кэш", "рубл", "доллар", "бакс", "доход", "потратил", "купил"
        ]
        text_lower = (message.text or message.caption or "").lower()
        if any(k in text_lower for k in pont_keywords):
            score = round(random.uniform(7.5, 9.9), 1)
            prompt = (
                f"Илья (@Akishinov) написал следующее сообщение: '{message.text}'.\n"
                f"Радар понтов зафиксировал уровень {score}/10! Приземли его ироничным, смешным и пацанским подколом про нефть, баррели, бабло или вахту (строго 1-2 предложения)."
            )
            try:
                response = await call_gemini_safe(
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(
                        temperature=0.8,
                        system_instruction=WISDOM_SYSTEM_INSTRUCTION
                    )
                )
                return f"🚨 <b>РАДАР ПОНТОВ:</b> Зафиксирован уровень понтов <b>{score}/10</b>!\n\n🧙 {html.escape(response.text.strip())}"
            except Exception as e:
                logging.error(f"Ошибка радара понтов: {e}")
    return ""


async def generate_text() -> dict:

    chat = await get_recent_messages()

    current_date_str = datetime.now(TZ).strftime("%d.%m.%Y")


    prompt = f"""
Каждое утро создавай короткую мысль дня на основе последних разговоров и указывай праздник.

Дата:
{current_date_str}

История сообщений чата (каждая строка записана в формате [Имя Автора]: сообщение):
{chat}

Правила аналитики текста:
- В истории разные участники пишут разное. Обращай внимание, КОМУ принадлежит фраза. НЕ смешивай мысли разных авторов в одну проблему.

Правила для JSON полей (СТРОГО):
- holiday: Напиши исключительно ОФИЦИАЛЬНЫЙ, общеизвестный реальный праздник или памятную дату, которая действительно выпадает на этот день ({current_date_str}) в мире или России. Категорически ЗАПРЕЩЕНО выдумывать шуточные или фейковые праздники на основе контекста разговоров.
- quote: Мудрая очень короткая мысль (1-2 предложения) с лёгким юмором и иронией, которая тонко обыгрывает настроение разговоров.

Дополнительные правила:
- мысль должна быть короткой, живой и вызывать улыбку;
- не использовать имена людей и кошачьих словечек;
- не упоминать Telegram, бота или технологии.
"""

    try:
        response = await call_gemini_safe(
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.8,
                system_instruction=WISDOM_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=DailyPostSchema
            )
        )

        raw_text = response.text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(raw_text)

    except Exception as e:
        logging.error(f"Gemini ошибка: {e}")
        return {
            "holiday": "День спокойных решений",
            "quote": "Даже самый длинный путь начинается с маленького шага, а иногда — с перезапуска утром."
        }


async def generate_summary() -> str:
    """Генерирует краткую выжимку переписки за последние 12 часов."""
    chat = await get_recent_messages(hours=12, limit=200)

    prompt = f"""
Посмотри переписку друзей за последнее время и сделай очень краткую, емкую выжимку в 3-4 буллитах (главные темы).

Переписка (формат [Имя Автора]: текст):
{chat}

Формат ответа:
• Тезис 1
• Тезис 2
• Тезис 3

Не используй реальные имена. Напиши лаконично и коротко.
"""
    try:
        response = await call_gemini_safe(
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.7,
                system_instruction=WISDOM_SYSTEM_INSTRUCTION
            )
        )
        return response.text.strip()
    except Exception as e:
        logging.error(f"Ошибка создания summary: {e}")
        return "Не удалось разобрать ваши перешёптывания — сервера перегружены."


async def generate_weekly_digest() -> str:
    """Дайджест итогов недели."""
    chat = await get_weekly_sampled_messages()

    prompt = f"""
Настали итоги недели! Проанализируй историю чата за неделю и составь короткий, красивый и смешной дайджест.

История:
{chat}

Составь небольшой пост из 3 частей:
1. 🏆 Главная тема недели
2. 🎭 Атмосфера недели (в 1-2 предложениях)
3. 📜 Напутствие Мяумяуры на следующую неделю (короткий мудрый совет)

Пиши без имён пользователей, с лёгкой иронией, лаконично.
"""
    try:
        response = await call_gemini_safe(
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.8,
                system_instruction=WISDOM_SYSTEM_INSTRUCTION
            )
        )
        return response.text.strip()
    except Exception as e:
        logging.error(f"Ошибка недели: {e}")
        return "Неделя прошла так стремительно, что даже мудрец не успел её осмыслить."


async def maybe_join_conversation(last_user: types.User | None = None) -> str:
    """Анализирует контекст беседы и решает, стоит ли вклиниться в разговор как живой друг."""
    chat = await get_recent_messages(hours=1, limit=8)
    if chat == "В чате было спокойно.":
        return ""

    user_name = last_user.first_name if last_user else "Участник"
    username_tag = f"@{last_user.username}" if last_user and last_user.username else user_name

    prompt = f"""
Вот последние сообщения в чате:
{chat}

Ты собираешься отреагировать на реплику пользователя {user_name} ({username_tag}).
1. Вникни в суть разговора и ответь уверенно, по-пацански и без подхалимажа, помня роли каждого в банде (Захар — главный кент, Паша — служит на Севере на Святом Носе в Баренцевом море, Илья — нефтяник для подколов).
2. Если уместно органично подключиться — напиши 1–2 живых предложения.
3. Если разговор сугубо личный, тема не располагает к вмешательству или сказать нечего — напиши строго одно слово: SKIP
"""
    try:
        response = await call_gemini_safe(
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.8,
                system_instruction=WISDOM_SYSTEM_INSTRUCTION
            )
        )
        text = response.text.strip()
        if "SKIP" in text or not text:
            return ""
        return text
    except Exception as e:
        logging.error(f"Ошибка спонтанного комментария: {e}")
        return ""


# =====================================================
# POST CREATORS
# =====================================================

async def create_daily_post(force=False):
    async with post_lock:
        if not force:
            if await was_post_today():
                logging.info("Пост уже отправлялся сегодня")
                return

        try:
            # Параллельно получаем мысль от Gemini и точную погоду от Open-Meteo
            gemini_task = generate_text()
            kultuk_task = get_weather_day(KULTUK_LAT, KULTUK_LON)
            svyatoy_task = get_weather_day(SVYATOY_NOS_LAT, SVYATOY_NOS_LON)

            data, kultuk, svyatoy = await asyncio.gather(
                gemini_task,
                kultuk_task,
                svyatoy_task
            )

            safe_holiday = html.escape(data.get('holiday', ''))
            safe_quote = html.escape(data.get('quote', ''))

            # Безопасное извлечение погоды с дефолтным значением
            k_morning = kultuk.get('🌅 Утро', 'нет данных')
            k_day = kultuk.get('☀️ День', 'нет данных')
            k_evening = kultuk.get('🌆 Вечер', 'нет данных')

            s_morning = svyatoy.get('🌅 Утро', 'нет данных')
            s_day = svyatoy.get('☀️ День', 'нет данных')
            s_evening = svyatoy.get('🌆 Вечер', 'нет данных')

            text = (
                f"📅 <b>Сегодня:</b>\n"
                f"{safe_holiday}\n\n"

                f"🌤 <b>Култук</b>\n"
                f"🌅 Утро: {k_morning}\n"
                f"☀️ День: {k_day}\n"
                f"🌆 Вечер: {k_evening}\n\n"

                f"🌊 <b>Святой Нос</b>\n"
                f"🌅 Утро: {s_morning}\n"
                f"☀️ День: {s_day}\n"
                f"🌆 Вечер: {s_evening}\n\n"

                f"✨ <b>Мысль дня от Мудреца Мяумяура:</b>\n"
                f"«{safe_quote}»"
            )

            await bot.send_message(CHAT_ID, text)

            if not force:
                await save_post_today()

            logging.info("Мысль дня и погода отправлены")

        except Exception as e:
            logging.exception(f"Ошибка создания поста: {e}")


async def send_weekly_digest():
    """Отправка недельного дайджеста по расписанию."""
    try:
        digest = await generate_weekly_digest()
        safe_digest = html.escape(digest)
        await bot.send_message(
            CHAT_ID,
            f"📊 <b>Итоги недели от Мудреца Мяумяура:</b>\n\n{safe_digest}"
        )
        logging.info("Недельный дайджест успешно отправлен")
    except Exception as e:
        logging.error(f"Ошибка отправки дайджеста: {e}")


# =====================================================
# COMMANDS
# =====================================================

@dp.message(Command("start"))
async def start(
    message: types.Message
):

    await message.answer(
        "🧙 Мудрец Мяумяура запущен.\n\n"
        "📜 <b>Мои возможности:</b>\n"
        "• Каждое утро в 09:00 — Мысль дня и погода\n"
        "• /summary — Краткая выжимка (что пропустили в чате)\n"
        "• /пробей <вопрос> — Разрулить любой спор по фактам с пруфами\n"
        "• Каждое воскресенье в 20:00 — Итоги недели\n"
        "• Кидай голосовые (я расшифрую) и фотки/кружочки (я заценю)!"
    )



@dp.message(Command("test"))
async def test(
    message: types.Message
):

    if ADMIN_ID and str(message.from_user.id) != str(ADMIN_ID):
        await message.reply("🧙 Только хранитель знаний (администратор) может вызывать это испытание.")
        return

    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)

    await message.answer(
        "🧠 Создаю тестовую мысль..."
    )


    await create_daily_post(
        force=True
    )


@dp.message(Command("summary"))
async def summary_cmd(
    message: types.Message
):
    """Краткая выжимка того, что произошло в чате за 12 часов."""
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)

    status_msg = await message.reply("🔮 Вспоминаю, о чём вы шептались...")

    summary_text = await generate_summary()
    safe_summary = html.escape(summary_text)

    await status_msg.edit_text(
        f"📜 <b>Краткая выжимка разговоров:</b>\n\n{safe_summary}"
    )


@dp.message(Command("probe", "пробей", "факт", "пруф", "спор", "чек"))
async def probe_cmd(message: types.Message):
    """Фактчекер споров по фактам с выходом в поиск."""
    parts = message.text.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    if not query and message.reply_to_message and message.reply_to_message.text:
        query = message.reply_to_message.text.strip()
    
    if not query:
        await message.reply("Напиши тему спора или вопрос, например: <code>/пробей сколько весит баррель нефти</code>")
        return

    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    verdict = await fact_check_dispute(query)
    await message.reply(f"⚖️ <b>Вердикт Мудреца:</b>\n\n{html.escape(verdict)}")


# =====================================================
# MEDIA & VOICE HANDLERS
# =====================================================

@dp.message(F.voice)
async def handle_voice_message(message: types.Message):
    """Моментальная расшифровка голосовых сообщений."""
    if message.chat.id != CHAT_ID:
        return

    username = message.from_user.first_name if message.from_user and message.from_user.first_name else "Участник"
    try:
        file = await bot.get_file(message.voice.file_id)
        voice_buffer = io.BytesIO()
        await bot.download_file(file.file_path, destination=voice_buffer)
        voice_bytes = voice_buffer.getvalue()

        transcribed = await transcribe_voice_bytes(voice_bytes)
        if transcribed:
            await save_message(username, f"[Голосовое сообщение]: {transcribed}")
            await message.reply(f"🗣 <b>{html.escape(username)}</b> (расшифровка):\n<i>«{html.escape(transcribed)}»</i>")
            
            # Если в расшифровке обратились к боту — отвечаем сразу
            bot_info = await bot.get_me()
            bot_username = bot_info.username.lower() if bot_info.username else ""
            if "мяумяура" in transcribed.lower() or (bot_username and f"@{bot_username}" in transcribed.lower()):
                await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
                answer = await ask_wisdom(transcribed, message.from_user)
                await message.answer(f"🧙 {html.escape(answer)}")
    except Exception as e:
        logging.error(f"Ошибка обработки войса: {e}")


@dp.message(F.photo | F.video_note)
async def handle_media_vision(message: types.Message):
    """Gemini Vision: реакция на фото и видео-кружочки."""
    if message.chat.id != CHAT_ID:
        return

    username = message.from_user.first_name if message.from_user and message.from_user.first_name else "Участник"
    caption = message.caption or ""

    # Проверяем, адресовано ли боту
    bot_info = await bot.get_me()
    is_reply_to_bot = bool(
        message.reply_to_message 
        and message.reply_to_message.from_user 
        and message.reply_to_message.from_user.id == bot_info.id
    )
    bot_username = bot_info.username.lower() if bot_info.username else ""
    is_addressed = "мяумяура" in caption.lower() or any(w in caption.lower() for w in ["зацени", "глянь", "посмотри", "оцени"]) or (bot_username and f"@{bot_username}" in caption.lower())

    if is_reply_to_bot or is_addressed:
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        try:
            media_buffer = io.BytesIO()
            if message.photo:
                file = await bot.get_file(message.photo[-1].file_id)
                await bot.download_file(file.file_path, destination=media_buffer)
                mime = "image/jpeg"
            elif message.video_note:
                file = await bot.get_file(message.video_note.file_id)
                await bot.download_file(file.file_path, destination=media_buffer)
                mime = "video/mp4"
            else:
                return

            comment = await analyze_visual_media(media_buffer.getvalue(), mime, caption, message.from_user)
            await message.reply(f"🧙 {html.escape(comment)}")
            await save_message(username, f"[Медиа]: {caption} (Бот: {comment})")
        except Exception as e:
            logging.error(f"Ошибка Vision: {e}")


# =====================================================
# CHAT COLLECTOR & RANDOM INTERACTIVE
# =====================================================

@dp.message()
async def collect(
    message: types.Message
):
    global last_random_comment_time

    # только нужный чат
    if message.chat.id != CHAT_ID:
        return


    # только текст
    if not message.text:
        return


    # игнорируем команды
    if message.text.startswith("/"):
        return


    # игнорируем ботов
    if (
        message.from_user
        and message.from_user.is_bot
    ):
        return



    username = (

        message.from_user.first_name

        if message.from_user
        and message.from_user.first_name

        else "Участник"

    )


    try:
        await save_message(
            username,
            message.text
        )


        logging.info(
            f"{username}: {message.text}"
        )

        # 1. Проверяем Радар понтов для Ильи
        ponto_alert = await check_ilya_ponto_radar(message)
        if ponto_alert:
            await message.reply(ponto_alert)
            return

        # 2. Быстрый пересказ ссылок (если прислали ссылку и тегнули/спросили или прямо в сообщении)
        url_match = re.search(r'https?://\S+', message.text)
        if url_match:
            is_asking_link = any(w in message.text.lower() for w in ["зацени", "глянь", "че там", "о чем", "перескажи", "суть", "мяумяура"])
            if is_asking_link:
                await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
                summary_link = await summarize_media_url(url_match.group(0), message.from_user)
                await message.reply(f"🎬 <b>Суть ссылки:</b>\n{html.escape(summary_link)}")
                return

        # 3. Проверяем прямое обращение (по имени «мяумяура» или Reply)
        bot_info = await bot.get_me()
        is_reply_to_bot = bool(
            message.reply_to_message 
            and message.reply_to_message.from_user 
            and message.reply_to_message.from_user.id == bot_info.id
        )
        bot_username = bot_info.username.lower() if bot_info.username else ""
        is_addressed = "мяумяура" in message.text.lower() or (bot_username and f"@{bot_username}" in message.text.lower())

        # Прямой вызов — ответ сразу
        if is_reply_to_bot or is_addressed:
            await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
            reply_quote = message.reply_to_message.text if message.reply_to_message and message.reply_to_message.text else None
            answer = await ask_wisdom(message.text, message.from_user, reply_quote)
            
            # Голосовой ответ ТОЛЬКО по прямой просьбе
            should_voice = any(w in message.text.lower() for w in ["голос", "скажи", "озвуч", "войс", "наговори", "проговори"])
            if should_voice:
                await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.RECORD_VOICE)
                audio_bytes = await text_to_speech_bytes(answer)
                if audio_bytes:
                    voice_file = BufferedInputFile(audio_bytes, filename="wisdom.mp3")
                    await message.reply_voice(voice=voice_file, caption=f"🧙 {html.escape(answer)}")
                    return

            await message.reply(f"🧙 {html.escape(answer)}")
            return

        # 4. Спонтанное вклинивание в обычный диалог (шанс 10%, не чаще раза в 10 минут)
        now = datetime.now(TZ)
        if (now - last_random_comment_time) > timedelta(minutes=10):
            if random.random() < 0.10:  # 10% шанс оценить контекст беседы
                remark = await maybe_join_conversation(message.from_user)
                if remark:
                    last_random_comment_time = now
                    await message.reply(f"🧙 {html.escape(remark)}")


    except Exception as e:
        logging.error(
            f"Ошибка сохранения сообщения: {e}"
        )


# =====================================================
# SCHEDULER
# =====================================================

def setup_scheduler():

    # Каждый день 09:00 Иркутск — Мысль дня и погода
    scheduler.add_job(
        create_daily_post,
        "cron",
        hour=9,
        minute=0,
        kwargs={
            "force": False
        },
        id="daily_post",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True
    )

    # Воскресенье 20:00 Иркутск — Итоги недели
    scheduler.add_job(
        send_weekly_digest,
        "cron",
        day_of_week="sun",
        hour=20,
        minute=0,
        id="weekly_digest",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True
    )

    # Очистка базы раз в неделю (Воскресенье 04:00 Иркутск)
    scheduler.add_job(
        clear_old_messages,
        "cron",
        day_of_week="sun",
        hour=4,
        minute=0,
        id="database_cleanup",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True
    )

    scheduler.start()

    logging.info(
        "Планировщик запущен: Иркутск 09:00 (daily), Вс 20:00 (weekly)"
    )


# =====================================================
# MAIN
# =====================================================

async def handle(request):
    return web.Response(text="Bot is running", status=200)


async def start_web_server():
    global web_runner
    app = web.Application()
    app.router.add_get('/', handle)
    
    port = int(os.getenv("PORT", 8080))
    web_runner = web.AppRunner(app)
    await web_runner.setup()
    site = web.TCPSite(web_runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Веб-сервер успешно запущен на порту {port}")


async def main():

    await init_db()

    setup_scheduler()

    await start_web_server()


    logging.info(
        "🧙 Мудрец Мяумяура работает"
    )


    try:


        await dp.start_polling(

            bot,

            skip_updates=True

        )



    finally:


        logging.info(
            "Остановка бота..."
        )


        scheduler.shutdown(
            wait=True
        )


        if web_runner:
            await web_runner.cleanup()


        if db_conn is not None:

            await db_conn.close()


        if http_session and not http_session.closed:

            await http_session.close()



        await bot.session.close()



# =====================================================
# ENTRY POINT
# =====================================================

if __name__ == "__main__":

    asyncio.run(main())
