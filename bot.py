from aiohttp import web 
import asyncio
import logging
import os
import json
import html
import random

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
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

gemini_lock = asyncio.Lock()


# Время последнего случайного комментария (чтобы не спамить)
last_random_comment_time = datetime.min.replace(tzinfo=TZ)


# Глобальная переменная для корректной остановки веб-сервера
web_runner = None


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
            SELECT username,text
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
            f"{user}: {text}"
            for user, text in rows
        ]
    )


    # защита лимита Gemini
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


        await db_conn.commit()


# =====================================================
# COMMANDS
# =====================================================

@dp.message(Command("start"))
async def start(
    message: types.Message
):

    await message.answer(
        "🧙 Мудрец Мяумура запущен.\n\n"
        "📜 <b>Мои возможности:</b>\n"
        "• Каждое утро в 09:00 — Мысль дня\n"
        "• /question &lt;вопрос&gt; — Спросить мудреца\n"
        "• /summary — Краткая выжимка (что пропустили в чате)\n"
        "• Каждое воскресенье в 20:00 — Итоги недели"
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

    answer = await ask_wisdom(question_text)
    
    safe_answer = html.escape(answer)
    await message.reply(
        f"🧙 <b>Мудрец Мяумура молвит:</b>\n\n{safe_answer}"
    )


@dp.message(Command("summary"))
async def summary_cmd(
    message: types.Message
):
    """Краткая выжимка того, что произошло в чате за 12 часов."""
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

        # Случайный мудрый комментарий в разговор (шанс 1%, но не чаще 1 раза в 30 мин)
        now = datetime.now(TZ)
        if (now - last_random_comment_time) > timedelta(minutes=30):
            if random.random() < 0.01:  # 1% шанс
                last_random_comment_time = now
                comment = await generate_random_remark(message.text)
                if comment:
                    await message.reply(f"🧙 <i>{html.escape(comment)}</i>")


    except Exception as e:
        logging.error(
            f"Ошибка сохранения сообщения: {e}"
        )



# =====================================================
# GEMINI FUNCTIONS
# =====================================================

async def ask_wisdom(user_question: str) -> str:

    prompt = f"""
Ты — древний и ироничный мудрец Мяумура из Иркутска.
Тебе задали вопрос: "{user_question}"

Дай короткий (2-4 предложения), глубинно-мудрый, философский, но при этом с лёгкой иронией и юмором ответ.
Не используй обращений по имени, не упоминай AI, Telegram и нейросети.
"""

    try:
        async with gemini_lock:
            response = await gemini.aio.models.generate_content(
                model="gemini-3-flash-preview",
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.8
                )
            )

        return response.text.strip()

    except Exception as e:
        logging.error(f"Gemini вопрос ошибка: {e}")
        return "Иногда лучший ответ — это молчание. Но сейчас просто произошла техническая заминка."


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
- holiday: Напиши исключительно ОФИЦИАЛЬНЫЙ, общеизвестный реальный праздник или памятную дату, которая действительно выпадает на этот день ({current_date_str}) в мире или России. Категорически ЗАПРЕЩЕНО выдумывать шуточные или фейковые праздники на основе контекста разговоров (никаких дней гулей и т.д.).
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
                model="gemini-3-flash-preview",
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.8,
                    response_mime_type="application/json",
                    response_schema=DailyPostSchema
                )
            )

        raw_text = response.text.strip()

        return json.loads(raw_text)


    except Exception as e:

        logging.error(
            f"Gemini ошибка: {e}"
        )


        return {
            "holiday": "День спокойных решений",
            "quote": "Даже самый длинный путь начинается с маленького шага, а иногда — с перезапуска утром."
        }


async def generate_summary() -> str:
    """Генерирует краткую выжимку переписки за последние 12 часов."""
    chat = await get_recent_messages(hours=12, limit=200)

    prompt = f"""
Ты — Мудрец Мяумура. Посмотри переписку друзей за последнее время и сделай краткую, с иронией и юмором выжимку в 3-5 буллитах (о чем спорили, что обсуждали, какие были главные темы).

Переписка:
{chat}

Формат ответа:
• Тезис 1
• Тезис 2
• Тезис 3

Не используй реальные имена. Напиши лаконично и живо.
"""
    try:
        async with gemini_lock:
            response = await gemini.aio.models.generate_content(
                model="gemini-3-flash-preview",
                contents=prompt,
                config=genai_types.GenerateContentConfig(temperature=0.7)
            )
        return response.text.strip()
    except Exception as e:
        logging.error(f"Ошибка создания summary: {e}")
        return "Не удалось разобрать ваши перешёптывания..."


async def generate_weekly_digest() -> str:
    """Дайджест итогов недели."""
    chat = await get_recent_messages(hours=168, limit=600)  # За 7 дней

    prompt = f"""
Ты — ироничный Мудрец Мяумура. Настали итоги недели!
Проанализируй историю чата за неделю и составь красивый, смешной дайджест.

История:
{chat}

Составь пост из 3 частей:
1. 🏆 Главная тема недели (что больше всего волновало чат)
2. 🎭 Атмосфера недели (в 1-2 предложениях)
3. 📜 Напутствие Мяумуры на следующую неделю (мудрый совет)

Пиши без имён пользователей, с лёгкой иронией и теплом.
"""
    try:
        async with gemini_lock:
            response = await gemini.aio.models.generate_content(
                model="gemini-3-flash-preview",
                contents=prompt,
                config=genai_types.GenerateContentConfig(temperature=0.8)
            )
        return response.text.strip()
    except Exception as e:
        logging.error(f"Ошибка недельного дайджеста: {e}")
        return "Неделя прошла так стремительно, что даже мудрец не успел её осмыслить."


async def generate_random_remark(msg_text: str) -> str:
    """Случайная реплика-комментарий к сообщению."""
    prompt = f"""
Ты — Мудрец Мяумура. Кто-то в чате только что сказал: "{msg_text}"
Напиши ровно 1 очень короткое, философское или смешное предложение-реплику в ответ.
"""
    try:
        async with gemini_lock:
            response = await gemini.aio.models.generate_content(
                model="gemini-3-flash-preview",
                contents=prompt,
                config=genai_types.GenerateContentConfig(temperature=0.9)
            )
        return response.text.strip()
    except Exception as e:
        return ""


# =====================================================
# POST CREATORS
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


            safe_holiday = html.escape(data['holiday'])
            safe_quote = html.escape(data['quote'])

            text = (

                f"📅 Сегодня:\n"
                f"{safe_holiday}\n\n"
                f"✨ Мысль дня:\n"
                f"«{safe_quote}»"

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


async def send_weekly_digest():
    """Отправка недельного дайджеста по расписанию."""
    try:
        digest = await generate_weekly_digest()
        safe_digest = html.escape(digest)
        await bot.send_message(
            CHAT_ID,
            f"📊 <b>Итоги недели от Мудреца Мяумуры:</b>\n\n{safe_digest}"
        )
        logging.info("Недельный дайджест успешно отправлен")
    except Exception as e:
        logging.error(f"Ошибка отправки дайджеста: {e}")


# =====================================================
# SCHEDULER
# =====================================================

def setup_scheduler():


    # Каждый день 09:00 Иркутск — Мысль дня
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
            wait=True
        )


        if web_runner:
            await web_runner.cleanup()


        if db_conn is not None:

            await db_conn.close()



        await bot.session.close()



# =====================================================
# ENTRY POINT
# =====================================================

if __name__ == "__main__":

    asyncio.run(main())
