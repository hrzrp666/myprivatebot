import asyncio
import json
import logging
import os

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
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

if not TELEGRAM_TOKEN or not GEMINI_KEY or not CHAT_ID:
    raise RuntimeError("Проверьте переменные очередей в .env")

CHAT_ID = int(CHAT_ID)
TZ = ZoneInfo("Asia/Irkutsk")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# =====================================================
# INIT
# =====================================================
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
gemini = genai.Client(api_key=GEMINI_KEY)

# Чистый планировщик без конфликтующих пулов потоков
scheduler = AsyncIOScheduler(timezone=TZ)

DB_PATH = "bot.db"

db_lock = asyncio.Lock()
post_lock = asyncio.Lock()
gemini_lock = asyncio.Lock()

# =====================================================
# GEMINI SCHEMA
# =====================================================
class DailyPostSchema(BaseModel):
    holiday: str
    quote: str

# =====================================================
# DATABASE
# =====================================================
async def connect_db():
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("PRAGMA journal_mode=WAL;")
    return db

async def init_db():
    async with db_lock:
        async with await connect_db() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT,
                    text TEXT,
                    created TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS posts(
                    date TEXT PRIMARY KEY
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_created 
                ON messages(created)
            """)
            await db.commit()
    logging.info("SQLite успешно инициализирована в режиме WAL")

async def save_message(username: str, text: str):
    async with db_lock:
        async with await connect_db() as db:
            await db.execute(
                "INSERT INTO messages (username, text, created) VALUES (?, ?, ?)",
                (username, text[:1000], datetime.now(TZ).isoformat())
            )
            await db.commit()

async def get_recent_messages() -> str:
    limit_time = (datetime.now(TZ) - timedelta(hours=24)).isoformat()
    async with db_lock:
        async with await connect_db() as db:
            async with db.execute(
                "SELECT username, text FROM messages WHERE created >= ? ORDER BY id DESC LIMIT 300",
                (limit_time,)
            ) as cursor:
                rows = await cursor.fetchall()

    if not rows:
        return "В чате было спокойно."

    rows.reverse()
    result = "\n".join([f"{user}: {text}" for user, text in rows])
    return result[-8000:]

async def clear_old_messages():
    limit_time = (datetime.now(TZ) - timedelta(days=30)).isoformat()
    async with db_lock:
        async with await connect_db() as db:
            await db.execute("DELETE FROM messages WHERE created < ?", (limit_time,))
            await db.commit()
    logging.info("Старая история сообщений успешно очищена.")

async def was_post_today() -> bool:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    async with db_lock:
        async with await connect_db() as db:
            async with db.execute("SELECT date FROM posts WHERE date = ?", (today,)) as cursor:
                result = await cursor.fetchone()
    return result is not None

async def save_post_today():
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    async with db_lock:
        async with await connect_db() as db:
            await db.execute("INSERT OR IGNORE INTO posts (date) VALUES (?)", (today,))
            await db.commit()

# =====================================================
# COMMANDS & COLLECTOR
# =====================================================
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer(
        "🧙 Мудрец Мяумура запущен.\n\nЯ слушаю разговоры в группе и каждое утро создаю мысль дня."
    )

@dp.message(Command("test"))
async def test(message: types.Message):
    await message.answer("🧠 Создаю тестовую мысль...")
    await create_daily_post(force=True)

@dp.message()
async def collect(message: types.Message):
    # Если написали в ЛС бота — просто игнорируем сборщик текста, но не вешаем бота
    if message.chat.type == "private":
        return
        
    # Сюда попадают только сообщения из целевой группы
    if message.chat.id != CHAT_ID or not message.text or message.text.startswith("/"):
        return
    if message.from_user and message.from_user.is_bot:
        return

    username = message.from_user.first_name if message.from_user and message.from_user.first_name else "Участник"

    try:
        await save_message(username, message.text)
        logging.info(f"Сохранено сообщение от {username}")
    except Exception as e:
        logging.error(f"Ошибка сохранения сообщения: {e}")

# =====================================================
# GEMINI GENERATION
# =====================================================
async def generate_text() -> dict:
    chat = await get_recent_messages()
    current_date_str = datetime.now(TZ).strftime("%d.%m.%Y")

    prompt = f"""
Ты — мудрый участник компании друзей из Иркутска.
Каждое утро создавай короткую мысль дня на основе последних разговоров.

Дата:
{current_date_str}

Последние разговоры:
{chat}

Правила для JSON полей (СТРОГО):
- holiday: Напиши исключительно ОФИЦИАЛЬНЫЙ, реальный, государственный, международный или общепризнанный календарный праздник, который выпадает на {current_date_str}. Категорически ЗАПРЕЩЕНО придумывать праздники, писать абстракции вроде "Иркутское утро", "Обычный день", "Четверг" или связывать название праздника с чатом. Если в этот день нет крупного праздника, напиши строго одну из стандартных фраз: "Официальный календарный день" или "Трудовой будний день".
- quote: Мудрая короткая мысль (1-3 предложения) с лёгким юмором и иронией, которая тонко обыгрывает или учитывает настроение разговоров в чате. Вот здесь ты можешь шутить, подкалывать и иронизировать над темами из разговоров.

Дополнительные правила:
- мысль должна быть живой и вызывать улыбку;
- не использовать имена людей;
- не упоминать Telegram, бота или технологии;
- не копировать сообщения дословно.
"""
    try:
        async with gemini_lock:
            response = await gemini.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.8,
                    response_mime_type="application/json",
                    response_schema=DailyPostSchema
                )
            )

        if response.parsed:
            if hasattr(response.parsed, "model_dump"):
                return response.parsed.model_dump()
            elif isinstance(response.parsed, dict):
                return response.parsed
            else:
                return dict(response.parsed)

        return json.loads(response.text)

    except Exception as e:
        logging.error(f"Gemini ошибка: {e}")
        return {
            "holiday": "Трудовой будний день",
            "quote": "Даже самый длинный путь начинается с маленького шага, а иногда — с перезапуска утром."
        }

# =====================================================
# POST CREATOR
# =====================================================
async def create_daily_post(force=False):
    async with post_lock:
        if not force and await was_post_today():
            logging.info("Пост уже отправлялся сегодня.")
            return

        try:
            data = await generate_text()
            text = f"📅 Сегодня:\n{data['holiday']}\n\n✨ Мысль дня:\n«{data['quote']}»"
            
            await bot.send_message(CHAT_ID, text)
            await save_post_today()
            logging.info("Мысль дня успешно отправлена.")
        except Exception as e:
            logging.exception(f"Ошибка создания поста: {e}")

# =====================================================
# SCHEDULER SETUP
# =====================================================
def setup_scheduler():
    scheduler.add_job(
        create_daily_post,
        "cron",
        hour=9,
        minute=0,
        kwargs={"force": False},
        id="daily_post",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True
    )
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
    logging.info("Планировщик запущен: Иркутск 09:00")

# =====================================================
# MAIN
# =====================================================
async def main():
    await init_db()
    # Запускаем планировщик строго внутри асинхронного цикла событий main
    setup_scheduler()
    logging.info("🧙 Мудрец Мяумура запущен и готов к работе")

    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        logging.info("Остановка бота...")
        scheduler.shutdown(wait=False)
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
