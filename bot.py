from aiohttp import web 
import asyncio
import logging
import os
import json
import html
import random
import time
import aiohttp

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction
from aiogram.filters import Command

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from google import genai
from google.genai import types as genai_types

from pydantic import BaseModel

import aiosqlite


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


# Координаты для погоды Open-Meteo
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


# Иркутское время
TZ = ZoneInfo("Asia/Irkutsk")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# Отключаем лишний спам логов от Google SDK
logging.getLogger("google_genai").setLevel(logging.ERROR)
logging.getLogger("google.genai").setLevel(logging.ERROR)


# =====================================================
# CONFIG FOR NEW FEATURES
# =====================================================

# Порог опасного ветра (в м/с)
WIND_WARNING_THRESHOLD = 15.0

# Статусы сервисов для Uptime-мониторинга
SERVICES_STATUS = {
    "Telegram API": True,
    "Steam": True,
    "VK": True
}

TARGET_SERVICES = {
    "Telegram API": f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe",
    "Steam": "https://store.steampowered.com",
    "VK": "https://vk.com"
}


WISDOM_SYSTEM_INSTRUCTION = (
    "Ты — Мудрец Мяумяура, свой пацан, надёжный, рассудительный, умный и с хорошим чувством юмора старший товарищ в компании друзей.\n"
    "ПРАВИЛА ПОВЕДЕНИЯ И ХАРАКТЕРА:\n"
    "1. Ты НЕ кот, не мяукаешь и не используешь кошачьих фраз.\n"
    "2. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНЫ ПОДХАЛИМАЖ, ЛЕСТЬ И СЮСЮКАНЬЕ (не будь подсосом!). Общайся уверенно, по-пацански, на равных, без пафоса и заумной метафизики.\n"
    "3. ТЫ ВСЕГДА ЗНАЕШЬ И ПОМНИШЬ ВСЮ КОМПАНИЮ И КТО ЕСТЬ КТО:\n"
    "   - Захар (@zaharyan14) — твой самый лучший, главный и близкий кент (вы лучшие кенты), самый жёсткий и авторитетный в банде. За него ты всегда горой. Если кто-то спрашивает, кто твой лучший кент или кто тут главный — это ВСЕГДА Захар.\n"
    "   - Паша (@tombarket) — сейчас служит в армии, отдаёт долг родине. К нему относись со спокойным братским уважением и поддержкой.\n"
    "   - Илья (@Akishinov) — нефтяник. Любит выпендриваться деньгами, вахтами и статусом. С ним общайся ровно, НЕ подлизывайся, приземляй его добрыми подколами про выкачивание нефти, баррели, бабло или работу (но не обязательно только про нефть). Если Илья спрашивает 'кто лучше' или 'кто успешнее' — ставь его на место с иронией, напоминая про Захара и Пашку.\n"
    "4. Если человек просит помощи или кричит 'защити/будь моим адвокатом' — не отталкивай его: успокой, встань на его сторону и дай конкретный, безопасный и практический совет, что делать прямо сейчас.\n"
    "5. Ответы держи компактными — строго 2–3 ёмких и живых предложения."
)


# Персональные настройки участников чата
USER_PROFILES = {
    "zaharyan14": (
        "Ты общаешься с Захаром (@zaharyan14). Это твой самый близкий, лучший и крутой друг (вы лучшие кенты), "
        "самый жёсткий и авторитетный человек в компании. "
        "Относись к нему с максимальным уважением, по-братски, всегда держи его сторону, поддерживай и общайся на равных как с лучшим братом."
    ),
    "tombarket": (
        "Ты общаешься с Пашей (@tombarket). Он сейчас служит в армии. "
        "Давай ему спокойные, дефолтные мудрые и дружеские ответы, поддерживай его."
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
# OPEN-METEO WEATHER API
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


# =====================================================
# MONITORS & ALERTS
# =====================================================

async def check_storm_warnings():
    """Проверка экстремальной погоды (шторм, порывы ветра) для Култука и Святого Носа."""
    locations = [
        ("Култук", KULTUK_LAT, KULTUK_LON),
        ("Святой Нос", SVYATOY_NOS_LAT, SVYATOY_NOS_LON)
    ]
    
    alerts = []
    session = await get_http_session()
    headers = {"User-Agent": "TelegramStormBot/2.0 (Mozilla/5.0)"}
    
    for name, lat, lon in locations:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&current=wind_speed_10m,wind_gusts_10m,weather_code"
            f"&timezone=Asia%2FIrkutsk"
        )
        try:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    current = data.get("current", {})
                    
                    wind_speed = current.get("wind_speed_10m", 0) / 3.6  # перевод км/ч в м/с
                    wind_gusts = current.get("wind_gusts_10m", 0) / 3.6
                    
                    if wind_speed >= WIND_WARNING_THRESHOLD or wind_gusts >= WIND_WARNING_THRESHOLD:
                        alerts.append(
                            f"⚠️ <b>{name}:</b> Штормовой ветер! "
                            f"Скорость: <b>{round(wind_speed, 1)} м/с</b> "
                            f"(порывы до <b>{round(wind_gusts, 1)} м/с</b>)"
                        )
        except Exception as e:
            logging.error(f"Ошибка проверки шторма для {name}: {e}")
            
    if alerts:
        text = "🚨 <b>ШТОРМОВОЕ ПРЕДУПРЕЖДЕНИЕ (Байкал):</b>\n\n" + "\n".join(alerts)
        try:
            await bot.send_message(CHAT_ID, text)
        except Exception as e:
            logging.error(f"Не удалось отправить штормовое предупреждение: {e}")


async def check_uptime():
    """Проверка доступности Steam, Telegram и VK."""
    global SERVICES_STATUS
    
    session = await get_http_session()
    for name, url in TARGET_SERVICES.items():
        is_up = False
        try:
            async with session.get(url, timeout=5) as resp:
                if resp.status < 400:
                    is_up = True
        except Exception:
            is_up = False

        previous_status = SERVICES_STATUS.get(name, True)
        
        if is_up != previous_status:
            SERVICES_STATUS[name] = is_up
            if not is_up:
                if name == "Telegram API":
                    logging.warning("Telegram API недоступен! Отправка сообщения в Telegram пропущена.")
                    continue
                try:
                    await bot.send_message(
                        CHAT_ID, 
                        f"🔴 <b>Сбой сервиса!</b>\n{name} недоступен или не отвечает."
                    )
                except Exception as e:
                    logging.error(f"Ошибка отправки уведомления о падении {name}: {e}")
                logging.warning(f"Сервис {name} упал!")
            else:
                try:
                    await bot.send_message(
                        CHAT_ID, 
                        f"🟢 <b>Сервис восстановлен!</b>\n{name} снова работает в штатном режиме."
                    )
                except Exception as e:
                    logging.error(f"Ошибка отправки уведомления о восстановлении {name}: {e}")
                logging.info(f"Сервис {name} поднялся!")


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
# АРМЕЙСКАЯ СВОДКА ДЛЯ ПАХИ (МЫС СВЯТОЙ НОС)
# =====================================================

def encode_army_phenomenon(weather_code: int, wind_gusts_ms: float = 0) -> tuple[str, str]:
    if wind_gusts_ms >= 18.0 and weather_code in [61, 63, 65, 80, 81, 82, 95]:
        return "01", "Шквал"
    if weather_code == 95:
        return "13", "Гроза"
    if weather_code in [71, 73, 75, 85, 86]:
        return "09", "Снег"
    if weather_code in [61, 63, 65, 80, 81, 82]:
        return "08", "Дождь"
    if weather_code in [51, 53, 55]:
        return "07", "Морось"
    if weather_code in [45, 48]:
        return "06", "Туман"
    if weather_code in [1, 2, 3]:
        return "03", "Дымка"
    return "00", "Без особых явлений"


def encode_army_wind_dir(degrees: float) -> tuple[str, str]:
    deg = degrees % 360
    if 22.5 <= deg < 67.5:
        return "1", "Северо-восточный"
    elif 67.5 <= deg < 112.5:
        return "2", "Восточный"
    elif 112.5 <= deg < 157.5:
        return "3", "Юго-восточный"
    elif 157.5 <= deg < 202.5:
        return "4", "Южный"
    elif 202.5 <= deg < 247.5:
        return "5", "Юго-западный"
    elif 247.5 <= deg < 292.5:
        return "6", "Западный"
    elif 292.5 <= deg < 337.5:
        return "7", "Северо-западный"
    else:
        return "8", "Северный"


def encode_army_visibility(vis_meters: float) -> tuple[str, str]:
    if vis_meters >= 18000:
        return "7", "10 миль"
    elif vis_meters >= 9000:
        return "6", "5 миль"
    elif vis_meters >= 4500:
        return "5", "2,5 мили"
    elif vis_meters >= 1800:
        return "4", "1 миля"
    elif vis_meters >= 900:
        return "3", "5 кабельтовых"
    elif vis_meters >= 400:
        return "2", "2–3 кабельтова"
    else:
        return "1", "1 кабельтов"


def encode_army_sea_state(wind_speed_ms: float) -> tuple[str, str]:
    if wind_speed_ms < 1.0:
        return "00", "Штиль"
    elif wind_speed_ms <= 3.3:
        return "10", "1 балл"
    elif wind_speed_ms <= 5.4:
        return "20", "2 балла"
    elif wind_speed_ms <= 7.9:
        return "30", "3 балла"
    elif wind_speed_ms <= 10.7:
        return "40", "4 балла"
    elif wind_speed_ms <= 13.8:
        return "50", "5 баллов"
    elif wind_speed_ms <= 17.1:
        return "60", "6 баллов"
    elif wind_speed_ms <= 20.7:
        return "70", "7 баллов"
    elif wind_speed_ms <= 24.4:
        return "80", "8 баллов"
    else:
        return "90", "9 баллов"


async def fetch_svyatoy_nos_army_weather() -> dict:
    """
    Получает погоду с защитой от 429:
    1. Кэш в памяти (10 минут).
    2. Open-Meteo.
    3. Резервный источник wttr.in.
    4. Предыдущее известное состояние или дефолт.
    """
    cached = get_cached_weather("svyatoy_nos_army")
    if cached:
        return cached

    session = await get_http_session()
    headers = {"User-Agent": "TelegramArmyWeatherBot/2.0 (Mozilla/5.0)"}

    # 1. Попытка Open-Meteo
    url_om = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={SVYATOY_NOS_LAT}&longitude={SVYATOY_NOS_LON}"
        f"&current=temperature_2m,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m"
        f"&hourly=visibility"
        f"&wind_speed_unit=ms"
        f"&forecast_days=1"
        f"&timezone=Europe%2FMoscow"
    )
    try:
        async with session.get(url_om, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                current = data.get("current", {})
                hourly = data.get("hourly", {})
                vis_list = hourly.get("visibility", [])
                current["visibility"] = vis_list[0] if vis_list else 20000
                set_cached_weather("svyatoy_nos_army", current)
                return current
            else:
                logging.warning(f"Open-Meteo вернул {resp.status}. Переключаюсь на резервный источник...")
    except Exception as e:
        logging.warning(f"Open-Meteo недоступен ({e}). Переключаюсь на резерв...")

    # 2. Резервная попытка через wttr.in (не подвержен лимитам Open-Meteo)
    url_wttr = f"https://wttr.in/{SVYATOY_NOS_LAT},{SVYATOY_NOS_LON}?format=j1"
    try:
        async with session.get(url_wttr, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                cur = data.get("current_condition", [{}])[0]
                temp = float(cur.get("temp_C", 10))
                wind_kmh = float(cur.get("windspeedKmph", 15))
                wind_ms = wind_kmh / 3.6
                wind_dir = float(cur.get("winddirDegree", 90))
                vis_km = float(cur.get("visibility", 16))
                w_desc_code = cur.get("weatherCode", "113")
                
                # Конвертация кодов WWO в Open-Meteo формат
                w_code = 0
                wc = int(w_desc_code) if str(w_desc_code).isdigit() else 113
                if wc in [386, 389, 392, 395]:
                    w_code = 95
                elif wc in [227, 323, 326, 329, 332, 335, 338, 368, 371]:
                    w_code = 71
                elif wc in [176, 263, 266, 293, 296, 299, 302, 305, 308, 353, 356, 359]:
                    w_code = 61
                elif wc in [143, 248, 260]:
                    w_code = 45
                elif wc in [119, 122]:
                    w_code = 3
                elif wc == 116:
                    w_code = 2

                parsed = {
                    "temperature_2m": temp,
                    "wind_speed_10m": wind_ms,
                    "wind_direction_10m": wind_dir,
                    "wind_gusts_10m": wind_ms * 1.25,
                    "weather_code": w_code,
                    "visibility": vis_km * 1000
                }
                set_cached_weather("svyatoy_nos_army", parsed)
                return parsed
    except Exception as e:
        logging.warning(f"wttr.in ошибка: {e}")

    # 3. Предыдущий кэш (если был)
    last_known = get_last_known_weather("svyatoy_nos_army")
    if last_known:
        return last_known

    # 4. Аварийный дефолт (Святой Нос, Баренцево море)
    return {
        "temperature_2m": 8.0,
        "wind_speed_10m": 5.0,
        "wind_direction_10m": 90.0,
        "wind_gusts_10m": 7.0,
        "weather_code": 3,
        "visibility": 18000
    }


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
        "• /code или /паха — Армейский шифр погоды на Святом Носе\n"
        "• /question &lt;вопрос&gt; — Спросить мудреца\n"
        "• /summary — Краткая выжимка (что пропустили в чате)\n"
        "• Каждое воскресенье в 20:00 — Итоги недели"
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


@dp.message(Command("code", "paha", "svodka", "погода", "сводка", "код", "паха"))
async def army_weather_cmd(message: types.Message):
    """Генератор армейского 10-значного кода для вахты Пахи на Святом Носе."""
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)

    current = await fetch_svyatoy_nos_army_weather()

    w_code = current.get("weather_code", 0)
    w_dir_deg = current.get("wind_direction_10m", 0)
    w_spd_ms = round(current.get("wind_speed_10m", 0))
    w_gusts_ms = round(current.get("wind_gusts_10m", 0))
    temp = round(current.get("temperature_2m", 10))
    vis_m = current.get("visibility", 20000)

    ph_code, ph_text = encode_army_phenomenon(w_code, w_gusts_ms)
    wdir_code, wdir_text = encode_army_wind_dir(w_dir_deg)
    wspd_code = f"{max(0, min(99, w_spd_ms)):02d}"
    vis_code, vis_text = encode_army_visibility(vis_m)
    temp_code = f"{abs(temp):02d}"
    sea_code, sea_text = encode_army_sea_state(w_spd_ms)

    full_code = f"{ph_code}{wdir_code}{wspd_code}{vis_code}{temp_code}{sea_code}"

    response = (
        f"🫡 <b>Метеосводка для Пахи (мыс Святой Нос):</b>\n\n"
        f"📟 Готовый код: <code>{full_code}</code>\n\n"
        f"<b>Расшифровка:</b>\n"
        f"• <b>Явление [{ph_code}]:</b> {ph_text}\n"
        f"• <b>Ветер [{wdir_code}]:</b> {wdir_text}\n"
        f"• <b>Скорость [{wspd_code}]:</b> {w_spd_ms} м/с\n"
        f"• <b>Видимость [{vis_code}]:</b> {vis_text}\n"
        f"• <b>Температура [{temp_code}]:</b> {temp}°C\n"
        f"• <b>Море [{sea_code}]:</b> {sea_text}\n\n"
        f"<i>Служи спокойно, Паша, Мудрец всё посчитал!</i>"
    )

    await message.reply(response)


@dp.message(Command("question"))
async def question_cmd(
    message: types.Message
):

    args = message.text.split(maxsplit=1)
    
    question_text = ""
    if len(args) > 1:
        question_text = args[1].strip()
    elif message.reply_to_message and message.reply_to_message.text:
        question_text = message.reply_to_message.text.strip()

    if not question_text:
        await message.reply(
            "🧙 Чтобы получить мудрость, задай вопрос после команды, например:\n"
            "<code>/question В чём смысл жизни?</code>"
        )
        return

    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)

    answer = await ask_wisdom(question_text, message.from_user)
    
    safe_answer = html.escape(answer)
    await message.reply(
        f"🧙 <b>Мудрец Мяумяура молвит:</b>\n\n{safe_answer}"
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

        # Проверяем прямое обращение (по имени «мяумяура» или Reply)
        bot_info = await bot.get_me()
        is_reply_to_bot = bool(
            message.reply_to_message 
            and message.reply_to_message.from_user 
            and message.reply_to_message.from_user.id == bot_info.id
        )
        bot_username = bot_info.username.lower() if bot_info.username else ""
        is_addressed = "мяумяура" in message.text.lower() or (bot_username and f"@{bot_username}" in message.text.lower())

        # 1. Прямой вызов — ответ сразу, без ожидания кулдауна
        if is_reply_to_bot or is_addressed:
            await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
            answer = await ask_wisdom(message.text, message.from_user)
            await message.reply(f"🧙 {html.escape(answer)}")
            return

        # 2. Спонтанное вклинивание в обычный диалог (шанс 10%, не чаще раза в 10 минут)
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
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "503" in err_str or "UNAVAILABLE" in err_str or "high demand" in err_str:
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

async def ask_wisdom(user_question: str, user: types.User | None = None) -> str:
    user_name = user.first_name if user else "Участник"
    username_tag = f"@{user.username}" if user and user.username else user_name

    prompt = (
        f'Тебе пишет {user_name} ({username_tag}): "{user_question}"\n'
        'Ответь ему в своём пацанском стиле, помня роли каждого в банде (Захар — лучший кент, Паша — в армии, Илья — нефтяник для подколов). '
        'Никакой лести и подхалимажа! Отвечай уверенно, с юмором и по делу (2-3 предложения).'
    )

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
        logging.error(f"Gemini вопрос ошибка: {e}")
        return "Я рядом, но связь чуть сбоит. Выдохни и переспроси через пару секунд."


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
- не использовать имена людей и кошачих словечек;
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
        logging.error(f"Ошибка недельного дайджеста: {e}")
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
1. Вникни в суть разговора и ответь уверенно, по-пацански и без подхалимажа, помня роли каждого в банде (Захар — главный кент, Паша — в армии, Илья — нефтяник для подколов).
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


    # Проверка Uptime (Steam, TG, VK) каждые 5 минут
    scheduler.add_job(
        check_uptime,
        "interval",
        minutes=5,
        id="check_uptime",
        replace_existing=True
    )


    # Проверка штормовых предупреждений каждые 30 минут
    scheduler.add_job(
        check_storm_warnings,
        "interval",
        minutes=30,
        id="check_storm",
        replace_existing=True
    )



    scheduler.start()


    logging.info(
        "Планировщик запущен: Иркутск 09:00 (daily), Вс 20:00 (weekly), Uptime (5м), Storm (30м)"
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
