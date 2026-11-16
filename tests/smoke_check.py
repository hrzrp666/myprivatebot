"""Дымовая проверка бота без обращения к Telegram и Gemini.

Запуск: задайте TELEGRAM_TOKEN, GEMINI_API_KEY, CHAT_ID (любые тестовые значения)
и при желании DATABASE_URL (Postgres), затем: python tests/smoke_check.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import database  # noqa: E402
import weather  # noqa: E402


def check_weather_logic() -> None:
    resolve = weather._resolve_location

    def name(text: str) -> str:
        return resolve(text, None)[1]

    assert name("какая погода в иркутске") == "Иркутск", name("какая погода в иркутске")
    assert name("погода на култуке") .startswith("Култук")
    # раньше «нос» в «относительно» и «север» перебивали Култук
    assert name("относительно северного ветра на култуке") .startswith("Култук")
    # без города — Култук по умолчанию, а не «Работе»
    assert name("как дела на работе, какая погода").startswith("Култук")
    assert name("погода в красноярске") == "Красноярске"
    # Святого Носа больше нет
    assert "Свято" not in name("поясни за погодку, дежурство")

    text = "доброе утро, завтра на работу"
    assert not any(k in text for k in config.WEATHER_KEYWORDS), "утро не должно запускать погоду"
    assert any(k in "какая завтра погода" for k in config.WEATHER_KEYWORDS)
    print("weather logic OK")


async def check_database() -> None:
    backend = "postgres" if config.DATABASE_URL else "sqlite"
    await database.init_db()
    try:
        await database.save_message("Тест", "привет из смоук-теста")
        await database.save_message("Тест", "второе сообщение")

        recent = await database.get_recent_messages(hours=1, limit=10)
        assert "привет из смоук-теста" in recent, recent
        # порядок от старых к новым
        assert recent.index("привет") < recent.index("второе")

        weekly = await database.get_weekly_sampled_messages()
        assert "--- Дата:" in weekly and "второе сообщение" in weekly, weekly

        assert await database.was_post_today() is False
        await database.save_post_today()
        await database.save_post_today()  # повторная запись не должна падать
        assert await database.was_post_today() is True

        await database.clear_old_messages()
        assert "второе сообщение" in await database.get_recent_messages(hours=1, limit=10)
    finally:
        await database.close_db()
    print(f"database OK ({backend})")


if __name__ == "__main__":
    check_weather_logic()
    asyncio.run(check_database())
