import asyncio
import html
import logging
from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ai_service import generate_text, generate_weekly_digest
from config import CHAT_ID, KULTUK_LAT, KULTUK_LON, SVYATOY_NOS_LAT, SVYATOY_NOS_LON, TZ, TZ_MSK
from database import clear_old_messages, save_post_today, was_post_today
from weather import get_weather_day

scheduler = AsyncIOScheduler(timezone=TZ)
_post_lock = asyncio.Lock()


async def create_daily_post(bot: Bot, force: bool = False) -> None:
    async with _post_lock:
        if not force and await was_post_today():
            logging.info("Пост уже отправлялся сегодня")
            return

        try:
            data, kultuk, svyatoy = await asyncio.gather(
                generate_text(),
                get_weather_day(KULTUK_LAT, KULTUK_LON, tz=TZ),
                get_weather_day(SVYATOY_NOS_LAT, SVYATOY_NOS_LON, tz=TZ_MSK),
            )

            safe_holiday = html.escape(data.get("holiday", ""))
            safe_quote = html.escape(data.get("quote", ""))

            text = (
                f"📅 <b>Сегодня:</b>\n"
                f"{safe_holiday}\n\n"
                f"🌤 <b>Култук</b>\n"
                f"🌅 Утро: {kultuk.get('🌅 Утро', 'нет данных')}\n"
                f"☀️ День: {kultuk.get('☀️ День', 'нет данных')}\n"
                f"🌆 Вечер: {kultuk.get('🌆 Вечер', 'нет данных')}\n\n"
                f"🌊 <b>Святой Нос</b>\n"
                f"🌅 Утро: {svyatoy.get('🌅 Утро', 'нет данных')}\n"
                f"☀️ День: {svyatoy.get('☀️ День', 'нет данных')}\n"
                f"🌆 Вечер: {svyatoy.get('🌆 Вечер', 'нет данных')}\n\n"
                f"✨ <b>Мысль дня от Мудреца Мяумяура:</b>\n"
                f"«{safe_quote}»"
            )

            await bot.send_message(CHAT_ID, text)

            if not force:
                await save_post_today()

            logging.info("Мысль дня и погода отправлены")
        except Exception as e:
            logging.exception(f"Ошибка создания поста: {e}")


async def send_weekly_digest(bot: Bot) -> None:
    """Отправка недельного дайджеста по расписанию."""
    try:
        digest = await generate_weekly_digest()
        safe_digest = html.escape(digest)
        await bot.send_message(
            CHAT_ID,
            f"📊 <b>Итоги недели от Мудреца Мяумяура:</b>\n\n{safe_digest}",
        )
        logging.info("Недельный дайджест успешно отправлен")
    except Exception as e:
        logging.error(f"Ошибка отправки дайджеста: {e}")


def setup_scheduler(bot: Bot) -> None:
    # Каждый день 09:00 Иркутск — Мысль дня и погода
    scheduler.add_job(
        create_daily_post,
        "cron",
        hour=9,
        minute=0,
        kwargs={"bot": bot, "force": False},
        id="daily_post",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )

    # Воскресенье 20:00 Иркутск — Итоги недели
    scheduler.add_job(
        send_weekly_digest,
        "cron",
        day_of_week="sun",
        hour=20,
        minute=0,
        kwargs={"bot": bot},
        id="weekly_digest",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
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
        coalesce=True,
    )

    scheduler.start()
    logging.info("Планировщик запущен: Иркутск 09:00 (daily), Вс 20:00 (weekly)")


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=True)
