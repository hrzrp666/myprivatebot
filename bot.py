import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from config import PORT, TELEGRAM_TOKEN, setup_logging
from database import close_db, init_db
from handlers import router
from scheduler import setup_scheduler, shutdown_scheduler
from weather import close_http_session

setup_logging()

bot = Bot(
    token=TELEGRAM_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML"),
)
dp = Dispatcher()
dp.include_router(router)

_web_runner: web.AppRunner | None = None


async def _healthcheck(request: web.Request) -> web.Response:
    return web.Response(text="Bot is running", status=200)


async def start_web_server() -> None:
    global _web_runner
    app = web.Application()
    app.router.add_get("/", _healthcheck)

    _web_runner = web.AppRunner(app)
    await _web_runner.setup()
    site = web.TCPSite(_web_runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Веб-сервер успешно запущен на порту {PORT}")


async def main() -> None:
    await init_db()
    setup_scheduler(bot)
    await start_web_server()

    logging.info("🧙 Мудрец Мяумяура работает")

    try:
        # В aiogram 3 параметра skip_updates нет: накопившиеся апдейты сбрасываются так
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        logging.info("Остановка бота...")
        shutdown_scheduler()
        if _web_runner:
            await _web_runner.cleanup()
        await close_db()
        await close_http_session()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
