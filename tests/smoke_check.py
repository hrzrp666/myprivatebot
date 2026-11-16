"""Дымовая проверка бота без обращения к Telegram и Gemini.

Запуск: задайте TELEGRAM_TOKEN, GEMINI_API_KEY, CHAT_ID (любые тестовые значения)
и при желании DATABASE_URL (Postgres), затем: python tests/smoke_check.py
"""
import asyncio
import random
import re
import sys
from pathlib import Path
from datetime import date, datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_service  # noqa: E402
import brain  # noqa: E402
import cooldowns  # noqa: E402
import config  # noqa: E402
import database  # noqa: E402
import handlers  # noqa: E402
import mood  # noqa: E402
import stickers  # noqa: E402
import weather  # noqa: E402
import whosaid  # noqa: E402
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


class _ApiError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"api error {code}")
        self.code = code


def check_fun_pure() -> None:
    """Настроение дня, варианты для «Кто это сказал?», разбор ошибок Gemini, статистика."""
    day = date(2026, 11, 16)
    assert mood.mood_for(day) == mood.mood_for(day) and mood.mood_for(day) in mood.MOODS
    assert len({mood.mood_for(date(2026, 1, 1) + timedelta(days=i)) for i in range(30)}) > 1
    assert mood.today_mood()[0] in mood.mood_instruction()

    names = ["Захар", "Паша", "Илья", "Тест", "Вася", "Захар"]
    options = whosaid.build_options("Захар", names, random.Random(1))
    assert len(options) == 4 and "Захар" in options and len(set(options)) == 4
    assert sorted(whosaid.build_options("A", ["A", "B"])) == ["A", "B"]
    game_id = whosaid.new_game("A", ["A", "B"], datetime(2026, 11, 1, tzinfo=config.TZ))
    assert whosaid.get_game(game_id).answer == "A" and whosaid.get_game("nope") is None

    classify = ai_service.classify_gemini_error
    assert classify(_ApiError(404)) == "dead"
    assert classify(_ApiError(429)) == "overloaded" and classify(_ApiError(503)) == "overloaded"
    assert classify(_ApiError(400)) == "other"
    assert classify(Exception("429 RESOURCE_EXHAUSTED")) == "overloaded"
    assert classify(Exception("boom")) == "other"

    stats = ai_service.format_weekly_stats([("Захар", 10), ("Илья", 2)])
    assert stats == "Захар — 10 сообщ.\nИлья — 2 сообщ.", stats
    print("fun pure OK")


async def check_fun_with_db() -> None:
    # --- паузы переживают перезапуск ---
    cooldowns.reset_cache()
    assert await cooldowns.since("never") is None
    assert await cooldowns.try_acquire("x", timedelta(hours=1)) is True
    assert await cooldowns.try_acquire("x", timedelta(hours=1)) is False
    cooldowns.reset_cache()  # как после деплоя: читаем из базы
    assert await cooldowns.try_acquire("x", timedelta(hours=1)) is False
    assert await cooldowns.since("x") < timedelta(minutes=1)
    assert await cooldowns.try_acquire("x", timedelta(0)) is True

    # --- статистика и случайная цитата ---
    await database.save_message("Захар", "это очень длинная и содержательная фраза для игры")
    await database.save_message(config.BOT_NAME, "бот тоже говорит длинные фразы, но его быть не должно")
    await database.save_message("Паша", "[Фото]: подпись к фото, служебная пометка, не для игры")
    await database.save_message("Паша", "зацените https://example.com/very/long/link/for/the/game")
    counts = await database.get_user_message_counts(days=7)
    assert config.BOT_NAME not in [n for n, _ in counts], counts
    assert ("Тест", 2) in counts and ("Захар", 1) in counts and ("Паша", 2) in counts, counts
    assert [c for _, c in counts] == sorted((c for _, c in counts), reverse=True), counts
    for _ in range(5):
        author, text, said_at = await database.get_random_quote(days=30)
        assert author == "Захар" and "содержательная" in text and said_at.tzinfo is not None

    assert await whosaid.add_point("Илья") == 1
    assert await whosaid.add_point("Илья") == 2
    await whosaid.add_point("Паша")
    assert await whosaid.leaderboard() == [("Илья", 2), ("Паша", 1)]

    async def fake_radar(*args, **kwargs):
        return SimpleNamespace(text="Опять баррели считаешь?")

    async def broken(*args, **kwargs):
        raise RuntimeError("Gemini лежит")

    prompts = []

    async def fake_daily(contents=None, config=None, **kwargs):
        prompts.append(contents)
        return SimpleNamespace(text='{"holiday": "День народного единства", "quote": "Мысль"}')

    async def fake_digest(contents=None, config=None, **kwargs):
        prompts.append(contents)
        return SimpleNamespace(text="Дайджест")

    original = ai_service.call_gemini_safe
    try:
        # --- радар понтов: только @Akishinov и с паузой ---
        ai_service.call_gemini_safe = fake_radar
        ilya = SimpleNamespace(
            from_user=SimpleNamespace(username="Akishinov", first_name="Илья"),
            text="купил новую тачку за миллион",
            caption=None,
        )
        other_ilya = SimpleNamespace(
            from_user=SimpleNamespace(username="other", first_name="Илья"),
            text="купил новую тачку",
            caption=None,
        )
        assert await ai_service.check_ilya_ponto_radar(other_ilya) == ""
        alert = await ai_service.check_ilya_ponto_radar(ilya)
        assert "РАДАР ПОНТОВ" in alert and "баррели" in alert
        assert await ai_service.check_ilya_ponto_radar(ilya) == "", "второй раз подряд — пауза"

        # --- утренний пост: без выдуманного праздника, с настроением ---
        ai_service.call_gemini_safe = broken
        assert (await ai_service.generate_text())["holiday"] == ""
        ai_service.call_gemini_safe = fake_daily
        data = await ai_service.generate_text()
        assert data == {"holiday": "День народного единства", "quote": "Мысль"}
        assert mood.today_mood()[0] in prompts[-1]

        # --- итоги недели: статистика и номинации ---
        ai_service.call_gemini_safe = fake_digest
        assert await ai_service.generate_weekly_digest() == "Дайджест"
        assert "Тест — 2 сообщ." in prompts[-1] and "Номинации недели" in prompts[-1]
    finally:
        ai_service.call_gemini_safe = original

    # --- 404 по коду ошибки выкидывает модель, запрос уходит в следующую ---
    called = []

    async def fake_generate(model=None, contents=None, config=None):
        called.append(model)
        if model == "dead-model":
            raise _ApiError(404)
        return SimpleNamespace(text="ok")

    original_client = ai_service.gemini
    try:
        ai_service.gemini = SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))
        )
        response = await ai_service.call_gemini_safe("hi", models=["dead-model", "good-model"])
        assert response.text == "ok" and called == ["dead-model", "good-model"]
        assert "dead-model" in ai_service._DEAD_MODELS
    finally:
        ai_service.gemini = original_client
        ai_service._DEAD_MODELS.discard("dead-model")

    # --- фото идёт в мозг вместе с историей и настроением ---
    seen = {}

    async def fake_vision(contents=None, config=None, **kwargs):
        seen["contents"], seen["config"], seen["timeout"] = contents, config, kwargs.get("timeout")
        return SimpleNamespace(function_calls=None, candidates=[], text="Огонь фотка")

    original = brain.call_gemini_safe
    try:
        brain.call_gemini_safe = fake_vision
        photo = genai_types.Part.from_bytes(data=b"img", mime_type="image/jpeg")
        user = SimpleNamespace(first_name="Паша", username="tombarket", id=7)
        reply = await brain.ask_wisdom("зацени", user, media=photo, media_kind="фото")
        assert reply.text == "Огонь фотка"
        last = seen["contents"][-1]
        assert last.parts[0] is photo and "прислал фото" in last.parts[-1].text
        assert "НАСТРОЕНИЕ ДНЯ" in seen["config"].system_instruction
        assert seen["timeout"] == brain.MEDIA_TIMEOUT
    finally:
        brain.call_gemini_safe = original
    print("fun with db OK")


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
        await check_fun_with_db()
    finally:
        await database.close_db()
    print(f"database OK ({backend})")


if __name__ == "__main__":
    check_weather_logic()
    check_reply_quote()
    check_brain_pure()
    check_fun_pure()
    asyncio.run(check_database())
