import asyncio
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


if not TELEGRAM_TOKEN:
    raise RuntimeError("Нет TELEGRAM_TOKEN")

if not GEMINI_KEY:
    raise RuntimeError("Нет GEMINI_API_KEY")

if not CHAT_ID:
    raise RuntimeError("Нет CHAT_ID")


CHAT_ID = int(CHAT_ID)


# Иркутское время
TZ = ZoneInfo("Asia/Irkutsk")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# =====================================================
# INIT
# =====================================================

bot = Bot(
    token=TELEGRAM_TOKEN
)

dp = Dispatcher()


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

async def init_db():

    global db_conn

    db_conn = await aiosqlite.connect(
        DB_PATH,
        timeout=30
    )

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
        "SQLite готова"
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



async def get_recent_messages():

    limit_time = (
        datetime.now(TZ)
        -
        timedelta(hours=24)
    ).isoformat()


    async with db_lock:

        async with db_conn.execute(
            """
            SELECT username,text
            FROM messages
            WHERE created >= ?
            ORDER BY id DESC
            LIMIT 300
            """,
            (limit_time,)
        ) as cursor:

            rows = await cursor.fetchall()


    if not rows:

        return "В чате было спокойно."


    rows.reverse()


    result = "\n".join(
        [
            f"{user}: {text}"
            for user, text in rows
        ]
    )


    return result[-8000:]



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


        await db_conn.commit()# =====================================================
# COMMANDS
# =====================================================

@dp.message(Command("start"))
async def start(
    message: types.Message
):

    await message.answer(
        "🧙 Мудрец Мяумура запущен.\n\n"
        "Я слушаю разговоры и каждое утро создаю мысль дня."
    )



@dp.message(Command("test"))
async def test(
    message: types.Message
):

    await message.answer(
        "🧠 Создаю тестовую мысль..."
    )


    await create_daily_post(
        force=True
    )



# =====================================================
# CHAT COLLECTOR
# =====================================================

@dp.message()
async def collect(
    message: types.Message
):

    if message.chat.id != CHAT_ID:
        return


    if not message.text:
        return


    if message.text.startswith("/"):
        return


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


    except Exception as e:

        logging.error(
            f"Ошибка сохранения сообщения: {e}"
        )



# =====================================================
# GEMINI
# =====================================================

async def generate_text() -> dict:

    chat = await get_recent_messages()


    current_date = datetime.now(TZ)

    date_text = current_date.strftime(
        "%d.%m.%Y"
    )


    prompt = f"""
Ты — мудрый участник компании друзей из Иркутска.

Создай утреннюю мысль дня на основе последних разговоров.

Дата:
{date_text}


Разговоры:
{chat}


Верни строго JSON:

{{
"holiday": "праздник или памятная дата",
"quote": "короткая мудрая мысль"
}}


ВАЖНЫЕ ПРАВИЛА ДЛЯ holiday:

- Пиши только настоящий официальный праздник.
- Не выдумывай праздники.
- Не используй контекст чата для создания праздника.
- Запрещены варианты:
  "Иркутское утро",
  "День Иркутска",
  "День хорошего настроения",
  "День чата",
  "День друзей",
  любые придуманные названия.

Если нет известного официального праздника на эту дату,
пиши:

"Обычный день"


ВАЖНЫЕ ПРАВИЛА ДЛЯ quote:

- Это должна быть новая мысль каждый раз.
- Не используй одну и ту же фразу.
- Не используй шаблон "даже самый длинный путь начинается..."
- Можно лёгко шутить про Иркутск, утро, погоду и настроение.
- Можно учитывать разговоры из чата.
- Не использовать имена людей.
- Не упоминать Telegram.
- Не упоминать бота.
- Не упоминать технологии.
- Длина 1-3 предложения.


Сделай мысль живой.
Ответ только JSON.
"""


    try:

        async with gemini_lock:

            response = await gemini.aio.models.generate_content(

                model="gemini-2.5-flash",

                contents=prompt,

                config=genai_types.GenerateContentConfig(

                    temperature=1.0,

                    response_mime_type="application/json",

                    response_schema=DailyPostSchema
                )
            )


        if response.parsed:

            if hasattr(
                response.parsed,
                "model_dump"
            ):

                return response.parsed.model_dump()


            if isinstance(
                response.parsed,
                dict
            ):

                return response.parsed



        import json

        return json.loads(
            response.text
        )


    except Exception as e:

        logging.error(
            f"Gemini ошибка: {e}"
        )


        return {

            "holiday":
            "Обычный день",

            "quote":
            "Каждое утро даёт новый шанс сделать что-то лучше, чем вчера."
        }# =====================================================
# POST CREATOR
# =====================================================

async def create_daily_post(
    force=False
):

    async with post_lock:


        if not force:

            if await was_post_today():

                logging.info(
                    "Пост уже отправлялся сегодня"
                )

                return



        try:

            data = await generate_text()


            text = (

                f"📅 Сегодня:\n"
                f"{data['holiday']}\n\n"
                f"✨ Мысль дня:\n"
                f"«{data['quote']}»"

            )


            await bot.send_message(
                CHAT_ID,
                text
            )


            if not force:

                await save_post_today()



            logging.info(
                "Мысль дня отправлена"
            )



        except Exception as e:

            logging.exception(
                f"Ошибка создания поста: {e}"
            )



# =====================================================
# SCHEDULER
# =====================================================

def setup_scheduler():


    # Каждый день 09:00 Иркутск

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



    # Очистка базы раз в неделю
    # Воскресенье 04:00 Иркутск

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
        "Планировщик запущен: Иркутск 09:00"
    )



# =====================================================
# MAIN
# =====================================================

async def main():


    await init_db()


    setup_scheduler()


    logging.info(
        "🧙 Мудрец Мяумура работает"
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
            wait=False
        )


        if db_conn:

            await db_conn.close()



        await bot.session.close()



# =====================================================
# ENTRY POINT
# =====================================================

if __name__ == "__main__":

    asyncio.run(main())
