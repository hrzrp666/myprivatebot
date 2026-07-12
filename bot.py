from aiohttp import web 
import asyncio
import logging
import os
import json
import html

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.utils.markdown import hlink 

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


# Используем асинхронный клиент для неблокирующих вызовов
gemini = genai.aio.Client(
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


    # Таблица для ставок и споров чата
    await db_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bets(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            message_id INTEGER,
            user_id INTEGER,
            user_name TEXT,
            target_user_id INTEGER,
            target_user_name TEXT,
            team TEXT,
            stake TEXT,
            status TEXT DEFAULT 'active',
            created TEXT
        )
        """
    )


    await db_conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_messages_created
        ON messages(created)
        """
    )


    # Индекс для быстрого поиска активных споров
    await db_conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bets_status 
        ON bets(status)
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


# Команда спортивной аналитики ИИ
@dp.message(Command("predict"))
async def cmd_predict(message: types.Message):
    match_info = message.text.replace("/predict", "").strip()
    if not match_info:
        await message.answer("🔮 Укажите участников матча. Пример: <code>/predict Норвегия - Англия</code>")
        return

    waiting_msg = await message.answer("🔮 Мудрец Мяумура вглядывается в линии вероятностей...")
    prompt = f"Ты — спортивный аналитик и мудрый кот Мяумура. Сделай ироничный разбор матча: {match_info}. Назови фаворита и дай кошачий совет."

    try:
        async with gemini_lock:
            response = await gemini.aio.models.generate_content(model="gemini-3-flash-preview", contents=prompt)
        await waiting_msg.edit_text(response.text)
    except Exception as e:
        logging.error(f"Ошибка Gemini в predict: {e}")
        await waiting_msg.edit_text("🔮 Звезды спорта скрылись за тучами. Попробуйте позже.")


# Команда фиксации нового спора между игроками
@dp.message(Command("bet"))
async def cmd_bet(message: types.Message):
    if message.chat.id != CHAT_ID:
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("⚠️ Ответьте командой <code>/bet</code> на сообщение человека, с которым вы спорите.")
        return

    args = message.text.replace("/bet", "").strip().split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ Формат: <code>/bet &lt;команда&gt; &lt;ставка&gt;</code>\nПример: <code>/bet Норвегия на пиццу</code>")
        return

    team, stake = html.escape(args[0]), html.escape(args[1])
    user_id = message.from_user.id
    user_name = message.from_user.first_name or "Участник"
    target_user_id = message.reply_to_message.from_user.id
    target_user_name = message.reply_to_message.from_user.first_name or "Оппонент"

    if user_id == target_user_id:
        await message.answer("😸 Нельзя спорить с самим собой.")
        return

    bot_msg = await message.answer("📝 Записываю условия спора...")

    async with db_lock:
        await db_conn.execute(
            """
            INSERT INTO bets (chat_id, message_id, user_id, user_name, target_user_id, target_user_name, team, stake, created)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (CHAT_ID, bot_msg.message_id, user_id, user_name, target_user_id, target_user_name, team, stake, datetime.now(TZ).isoformat())
        )
        await db_conn.commit()

    # ИСПРАВЛЕНО: hlink сам экранирует имя внутри себя, поэтому убрано предварительное html.escape для имен.
    # Для отображения внутри блоков <code> имена экранируются отдельно.
    user_link = hlink(user_name, f"tg://user?id={user_id}")
    target_link = hlink(target_user_name, f"tg://user?id={target_user_id}")

    safe_u_name_code = html.escape(user_name)
    safe_t_name_code = html.escape(target_user_name)

    await bot_msg.edit_text(
        f"🤝 <b>Спор принят и записан в лапы!</b>\n\n"
        f"👤 {user_link} ставит на: <b>{team}</b>\n"
        f"🎯 Оппонент: {target_link}\n"
        f"💳 На кону: <b>{stake}</b>\n\n"
        f"Чтобы рассчитать этот спор, ответьте на ЭТО сообщение командой:\n"
        f"<code>/match_result 1</code> — если выиграл {safe_u_name_code}\n"
        f"<code>/match_result 2</code> — если выиграл {safe_t_name_code}"
    )


# Команда подведения итогов спора по message_id
@dp.message(Command("match_result"))
async def cmd_match_result(message: types.Message):
    if message.chat.id != CHAT_ID:
        return

    if not message.reply_to_message:
        await message.answer("⚠️ Ответьте командой <code>/match_result 1</code> или <code>/match_result 2</code> на сообщение бота, в котором он зафиксировал ваш спор.")
        return

    choice = message.text.replace("/match_result", "").strip()
    if choice not in ["1", "2"]:
        await message.answer("⚠️ Укажите победителя цифрой 1 или 2.")
        return

    reply_msg_id = message.reply_to_message.message_id

    async with db_lock:
        async with db_conn.execute(
            "SELECT id, user_name, target_user_name, team, stake, user_id, target_user_id FROM bets WHERE chat_id = ? AND message_id = ? AND status = 'active'",
            (CHAT_ID, reply_msg_id)
        ) as cursor:
            bet = await cursor.fetchone()

        if not bet:
            await message.answer("🐾 В моих архивах не найдено активного спора, связанного с этим сообщением.")
            return

        bet_id, u_name, t_name, team, stake, u_id, t_id = bet
        await db_conn.execute("UPDATE bets SET status = 'completed' WHERE id = ?", (bet_id,))
        await db_conn.commit()

    # ИСПРАВЛЕНО: hlink сам безопасно экранирует текст ссылки, убрано двойное экранирование.
    u_link = hlink(u_name, f"tg://user?id={u_id}")
    t_link = hlink(t_name, f"tg://user?id={t_id}")

    if choice == "1":
        debt_text = f"<b>{u_link}</b> оказался прав в своей ставке на <b>{team}</b>!\n💸 Проигравший {t_link} теперь обязан отдать: <b>{stake}</b>"
        winner = u_name
    else:
        debt_text = f"<b>{t_link}</b> оказался прав в своей ставке на <b>{team}</b>!\n💸 Проигравший {u_link} теперь обязан отдать: <b>{stake}</b>"
        winner = t_name

    prompt = f"Напиши короткую кошачью шутку-наставление от Мудреца Мяумура про то, что человек по имени {winner} победил в споре, а проигравший должен поскорее вернуть долг."
    try:
        async with gemini_lock:
            response = await gemini.aio.models.generate_content(model="gemini-3-flash-preview", contents=prompt)
        bot_comment = html.escape(response.text)
    except Exception as e:
        logging.error(f"Ошибка Gemini в match_result: {e}")
        bot_comment = "🐾 Судьба определила сильнейшего. Время платить по счетам!"

    await message.answer(f"{debt_text}\n\n🔮 <b>Слово Мяумура:</b>\n{bot_comment}")


# =====================================================
# CHAT COLLECTOR
# =====================================================

@dp.message()
async def collect(
    message: types.Message
):

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
        clean_text = html.escape(message.text)

        await save_message(
            username,
            clean_text
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
        if raw_text.startswith("```"):
            if raw_text.startswith("```json"):
                raw_text = raw_text[7:]
            else:
                raw_text = raw_text[3:]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3]
        raw_text = raw_text.strip()

        return json.loads(raw_text)


    except Exception as e:

        logging.error(
            f"Gemini ошибка: {e}"
        )


        return {
            "holiday": "День спокойных решений",
            "quote": "Даже самый длинный путь начинается с маленького шага, а иногда — с перезапуска утром."
        }


# =====================================================
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
            wait=False
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
