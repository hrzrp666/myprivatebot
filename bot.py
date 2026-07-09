import asyncio
import logging
import json
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


# =====================
# CONFIG
# =====================

load_dotenv()


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
CHAT_ID = os.getenv("CHAT_ID")


if not TELEGRAM_TOKEN:
    raise Exception("Нет TELEGRAM_TOKEN")

if not GEMINI_KEY:
    raise Exception("Нет GEMINI_API_KEY")

if not CHAT_ID:
    raise Exception("Нет CHAT_ID")


CHAT_ID = int(CHAT_ID)


TZ = ZoneInfo("Asia/Irkutsk")


logging.basicConfig(
    level=logging.INFO
)


# =====================
# INIT
# =====================

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



# =====================
# GEMINI SCHEMA
# =====================

class DailyPostSchema(BaseModel):
    holiday: str
    quote: str



# =====================
# DATABASE
# =====================


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
        CREATE INDEX IF NOT EXISTS idx_created
        ON messages(created)
        """
    )


    await db_conn.commit()


    logging.info(
        "База данных готова"
    )



async def save_message(
    username,
    text
):

    async with db_lock:

        await db_conn.execute(
            """
            INSERT INTO messages
            (username, text, created)
            VALUES (?, ?, ?)
            """,
            (
                username,
                text,
                datetime.now(TZ).isoformat()
            )
        )


        await db_conn.commit()



async def get_recent_messages():

    limit_time = (
        datetime.now(TZ)
        - timedelta(days=1)
    ).isoformat()


    async with db_lock:

        async with db_conn.execute(
            """
            SELECT username, text
            FROM messages
            WHERE created >= ?
            ORDER BY id DESC
            LIMIT 300
            """,
            (
                limit_time,
            )

        ) as cursor:

            rows = await cursor.fetchall()



    if not rows:

        return (
            "В чате было спокойно. "
            "Интересных тем почти не было."
        )


    rows.reverse()


    return "\n".join(
        [
            f"{user}: {text}"
            for user, text in rows
        ]
    )



# =====================
# COMMANDS
# =====================


@dp.message(Command("start"))
async def start(
    message: types.Message
):

    await message.answer(
        "🧙 Мудрец Мяумура работает.\n\n"
        "Я читаю чат и каждое утро "
        "создаю мысль дня."
    )



@dp.message(Command("test"))
async def test(
    message: types.Message
):

    await message.answer(
        "🧠 Создаю тестовую мудрость..."
    )


    await create_daily_post()



# =====================
# CHAT
# =====================


@dp.message(
    lambda message:
    message.text
    and not message.text.startswith("/")
)
async def collect_messages(
    message: types.Message
):

    if message.chat.id != CHAT_ID:
        return


    if message.from_user and message.from_user.is_bot:
        return


    username = (
        message.from_user.first_name
        if message.from_user
        else "Участник"
    )


    logging.info(
        f"{username}: {message.text}"
    )


    await save_message(
        username,
        message.text
    )



# =====================
# GEMINI
# =====================


async def generate_text():


    chat = await get_recent_messages()


    today = datetime.now(TZ)


    prompt = f"""

Ты — мудрый, но слегка ироничный участник компании друзей.

Каждое утро ты создаёшь "мысль дня"
по мотивам последних разговоров в чате.


Сегодня:
{today.strftime("%d.%m.%Y")}


Последние сообщения:

{chat}


Верни только JSON:

{{
"holiday":"реальный праздник сегодня",
"quote":"философская цитата с юмором"
}}


Правила цитаты:

- Она должна быть мудрой, но живой.
- Добавь лёгкую иронию.
- Она должна вызывать улыбку.
- Используй настроение или тему разговора.
- Не упоминай имена людей.
- Не упоминай Telegram, бота и технологии.
- Не копируй сообщения.
- Избегай общих фраз вроде "люди всегда стремятся".
- Цитата должна выглядеть как мысль человека из вашей компании.
- Длина: 1-3 предложения.


Ответ только JSON.
"""


    response = await gemini.aio.models.generate_content(

        model="gemini-2.5-flash",

        contents=prompt,

        config=genai_types.GenerateContentConfig(

            temperature=0.9,

            response_mime_type="application/json",

            response_schema=DailyPostSchema
        )
    )


    return json.loads(
        response.text
    )



# =====================
# POST
# =====================


async def create_daily_post():

    async with post_lock:

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


            logging.info(
                "Пост отправлен"
            )


        except Exception as e:

            logging.error(
                f"Ошибка поста: {e}"
            )



# =====================
# START
# =====================


async def main():

    await init_db()


    scheduler.add_job(
        create_daily_post,
        "cron",
        hour=9,
        minute=0
    )


    scheduler.start()


    logging.info(
        "🧙 Мудрец Мяумура запущен"
    )


    try:

        await dp.start_polling(
            bot
        )


    finally:

        scheduler.shutdown()


        if db_conn:

            await db_conn.close()



if __name__ == "__main__":

    asyncio.run(main())
