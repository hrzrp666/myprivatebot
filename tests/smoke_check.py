"""Дымовая проверка бота без обращения к Telegram и Gemini.

Запуск: задайте TELEGRAM_TOKEN, GEMINI_API_KEY, CHAT_ID (любые тестовые значения)
и при желании DATABASE_URL (Postgres), затем: python tests/smoke_check.py
"""
import asyncio
import re
import sys
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_service  # noqa: E402
import brain  # noqa: E402
import config  # noqa: E402
import database  # noqa: E402
import handlers  # noqa: E402
import stickers  # noqa: E402
import weather  # noqa: E402
from google.genai import types as genai_types  # noqa: E402


def check_reply_quote() -> None:
    bot_info = SimpleNamespace(id=1)

    def quote(text, user_id, first_name):
        msg = SimpleNamespace(
            text=text, caption=None, from_user=SimpleNamespace(id=user_id, first_name=first_name)
        )
        return handlers._format_reply_quote(msg, bot_info)

    assert quote("привет", 5, "Захар") == "[Захар]: привет"
    assert quote("ответ бота", 1, None) == "[Мудрец Мяумяура]: ответ бота"
    assert handlers._format_reply_quote(None, bot_info) is None
    empty = SimpleNamespace(text=None, caption=None, from_user=None)
    assert handlers._format_reply_quote(empty, bot_info) is None
    print("reply quote OK")


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

    print("weather logic OK")


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


async def check_long_memory() -> None:
    """Долгая память: хранилище kv, ручные факты и ежедневное обновление (Gemini подменён заглушкой)."""
    await database.set_kv("k", "one")
    await database.set_kv("k", "two")
    assert await database.get_kv("k") == "two"
    assert await database.get_kv("missing") == ""

    await ai_service.clear_memory()
    assert await ai_service.get_memory_context() == ""

    await ai_service.add_manual_fact("Илья вернулся с вахты")
    assert "Илья вернулся с вахты" in await ai_service.get_memory_context()
    for i in range(40):  # лимит длины: старые ручные факты вытесняются
        await ai_service.add_manual_fact(f"факт {i} " + "x" * 250)
    manual = await database.get_kv(ai_service.MANUAL_FACTS_KEY)
    assert len(manual) <= 1500 and "факт 39" in manual, len(manual)

    async def fake_good(*args, **kwargs):
        return _FakeResponse("• Захар живёт в Култуке и любит рыбалку. Илья работает нефтяником.")

    async def fake_short(*args, **kwargs):
        return _FakeResponse("ок")

    original = ai_service.call_gemini_safe
    try:
        ai_service.call_gemini_safe = fake_good
        assert await ai_service.update_long_memory() is True
        saved = await database.get_kv(ai_service.MEMORY_KEY)
        assert "Култуке" in saved

        ai_service.call_gemini_safe = fake_short  # слишком короткий ответ не затирает заметки
        assert await ai_service.update_long_memory() is False
        assert await database.get_kv(ai_service.MEMORY_KEY) == saved
    finally:
        ai_service.call_gemini_safe = original

    assert "Култуке" in await ai_service.get_memory_context()
    await ai_service.clear_memory()
    assert await ai_service.get_memory_context() == ""
    print("long memory OK")


def check_brain_pure() -> None:
    """Диалог ролями, разбор решения «отвечать ли», безопасные ссылки."""
    dt = datetime(2026, 11, 16, 18, 5, tzinfo=config.TZ)
    rows = [
        ("Захар", "привет", dt),
        (config.BOT_NAME, "здорово", dt),
        ("Паша", "как дела", dt),
        ("Илья", "норм", dt),
    ]
    contents = brain.build_dialogue(rows, "[ответь Илье]")
    assert [c.role for c in contents] == ["user", "model", "user"], [c.role for c in contents]
    last = contents[-1].parts[0].text
    assert "[16.11 18:05] Паша: как дела" in last and "Илья: норм" in last
    assert last.endswith("[ответь Илье]")
    assert contents[1].parts[0].text == "здорово"

    # диалог не начинается с реплики бота и всегда заканчивается пользователем
    contents = brain.build_dialogue([(config.BOT_NAME, "x", dt), ("Захар", "y", dt)], "note")
    assert [c.role for c in contents] == ["user"]
    contents = brain.build_dialogue([("Захар", "y", dt), (config.BOT_NAME, "x", dt)], "note")
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert brain.build_dialogue([], "note")[0].parts[0].text == "note"

    parse = brain.parse_decision
    d = parse('{"action": "react", "addressed": false, "emoji": "❤️"}')
    assert d.action == "react" and d.emoji == "❤", d
    assert parse('{"action": "react", "emoji": "🦖"}').action == "ignore"
    assert parse("полная ерунда").action == "ignore"
    assert parse('{"action": "dance"}').action == "ignore"
    d = parse('```json\n{"action": "reply", "addressed": true, "emoji": ""}\n```')
    assert d.action == "reply" and d.addressed is True

    safe = brain.is_safe_public_url
    assert safe("https://youtube.com/watch?v=1") and safe("http://example.com")
    for bad in ("http://localhost:8080", "http://127.0.0.1", "http://10.0.0.5/x",
                "http://169.254.169.254/latest", "ftp://example.com", "javascript:alert(1)"):
        assert not safe(bad), bad

    assert brain._extract_text(SimpleNamespace(text="[16.11 18:05] Мудрец Мяумяура: Здорово")) == "Здорово"
    assert brain._extract_text(SimpleNamespace(text="Мяумяура: Ну")) == "Ну"
    assert brain._extract_text(SimpleNamespace(text="Обычный ответ")) == "Обычный ответ"
    print("brain pure OK")


async def check_brain_with_db() -> None:
    """Инструменты, цикл вызова функций и решение «отвечать ли» с подставным Gemini."""
    reply = brain.BotReply()
    assert "нельзя" in await brain.run_tool("react", {"emoji": "🦖"}, reply) and reply.reaction is None
    await brain.run_tool("react", {"emoji": "🔥"}, reply)
    assert reply.reaction == "🔥"
    await brain.run_tool("reply_with_voice", {}, reply)
    assert reply.voice is True
    assert "нельзя" in await brain.run_tool("open_link", {"url": "http://127.0.0.1/admin"}, reply)
    assert "Неизвестная" in await brain.run_tool("hack", {}, reply)

    await ai_service.clear_memory()
    await brain.run_tool("remember_fact", {"fact": "Паша устроился на работу"}, reply)
    assert "Паша устроился на работу" in await ai_service.get_memory_context()
    await ai_service.clear_memory()

    assert "нет стикеров" in await brain.run_tool("send_sticker", {"emoji": "😂"}, reply)
    await stickers.remember_sticker("FILE1", "U1", "😂")
    await stickers.remember_sticker("FILE1", "U1", "😂")  # дубль не добавляется
    await stickers.remember_sticker("FILE2", "U2", "👍")
    assert await stickers.available_sticker_emojis() == ["😂", "👍"]
    assert "Такого стикера нет" in await brain.run_tool("send_sticker", {"emoji": "🐸"}, reply)
    await brain.run_tool("send_sticker", {"emoji": "😂"}, reply)
    assert reply.sticker_file_id == "FILE1"

    asked = []

    async def fake_weather(query, user=None):
        asked.append(query)
        return "Локация: Иркутск\nСейчас: -5°C, снег"

    calls = []

    async def fake_gemini(contents=None, config=None, **kwargs):
        calls.append(contents)
        if len(calls) == 1:
            return SimpleNamespace(
                function_calls=[SimpleNamespace(name="get_weather", args={"location": "Иркутск"})],
                candidates=[SimpleNamespace(content=genai_types.Content(
                    role="model", parts=[genai_types.Part(text="(вызов)")]))],
                text=None,
            )
        return SimpleNamespace(
            function_calls=None, candidates=[],
            text="[16.11 18:05] Мудрец Мяумяура: В Иркутске -5 и снег, одевайся теплее.",
        )

    async def broken_gemini(*args, **kwargs):
        raise RuntimeError("Gemini лежит")

    original_gemini, original_weather = brain.call_gemini_safe, brain.fetch_dynamic_weather
    try:
        brain.call_gemini_safe, brain.fetch_dynamic_weather = fake_gemini, fake_weather
        user = SimpleNamespace(first_name="Захар", username="zaharyan14", id=5)
        answer = await brain.ask_wisdom("какая погода в иркутске?", user)
        assert answer.text == "В Иркутске -5 и снег, одевайся теплее.", answer.text
        assert answer.tools_used == ["get_weather"] and asked == ["погода в Иркутск"]
        # во второй запрос ушли: история + вызов модели + результат функции
        second = calls[1]
        assert second[-1].parts[0].function_response.name == "get_weather"
        assert second[-1].parts[0].function_response.response["result"].startswith("Локация: Иркутск")

        brain.call_gemini_safe = broken_gemini
        assert (await brain.ask_wisdom("эй", user)).text == brain.FALLBACK_TEXT
        assert (await brain.ask_wisdom("эй", user, spontaneous=True)).is_empty
        assert (await brain.decide_engagement(None)).action == "ignore"

        async def decide_reply(*args, **kwargs):
            assert kwargs.get("models") == ai_service.LIGHT_MODELS
            return SimpleNamespace(text='{"action": "reply", "addressed": true, "emoji": ""}')

        brain.call_gemini_safe = decide_reply
        decision = await brain.decide_engagement(12.0)
        assert decision.action == "reply" and decision.addressed is True
    finally:
        brain.call_gemini_safe, brain.fetch_dynamic_weather = original_gemini, original_weather
    print("brain with db OK")


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

        timed = await database.get_recent_messages(hours=1, limit=10, with_time=True)
        assert re.search(r"\[\d{2}\.\d{2} \d{2}:\d{2}\] \[Тест\]: привет", timed), timed

        weekly = await database.get_weekly_sampled_messages()
        assert "--- Дата:" in weekly and "второе сообщение" in weekly, weekly

        assert await database.was_post_today() is False
        await database.save_post_today()
        await database.save_post_today()  # повторная запись не должна падать
        assert await database.was_post_today() is True

        await database.clear_old_messages()
        assert "второе сообщение" in await database.get_recent_messages(hours=1, limit=10)

        await check_long_memory()
        await check_brain_with_db()
    finally:
        await database.close_db()
    print(f"database OK ({backend})")


if __name__ == "__main__":
    check_weather_logic()
    check_reply_quote()
    check_brain_pure()
    asyncio.run(check_database())
