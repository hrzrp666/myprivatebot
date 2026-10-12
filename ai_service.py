import asyncio
import html
import json
import logging
import random
import re
from datetime import datetime
import aiohttp
from aiogram import types
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel

from config import (
    GEMINI_KEY,
    PONT_KEYWORDS,
    TZ,
    WEATHER_DIRECT_KEYWORDS,
    WEATHER_KEYWORDS,
    WISDOM_SYSTEM_INSTRUCTION,
    get_user_context,
)
from database import get_recent_messages, get_weekly_sampled_messages
from weather import fetch_dynamic_weather, get_http_session

gemini = genai.Client(api_key=GEMINI_KEY)


class DailyPostSchema(BaseModel):
    holiday: str
    quote: str


import time

_MODELS_PRIORITY = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-flash-lite",
    "gemini-3-flash-preview",
]
# Мемоизация 404-моделей и временный кулдаун для перегруженных моделей
_DEAD_MODELS: set[str] = set()
_COOLDOWN_UNTIL: dict[str, float] = {}
_preferred_model: str | None = None

# Предварительно созданные конфигурации Gemini (без повторной аллокации Pydantic-объектов)
_CFG_AUDIO_02 = genai_types.GenerateContentConfig(temperature=0.2)
_CFG_WISDOM_03 = genai_types.GenerateContentConfig(
    temperature=0.3,
    system_instruction=WISDOM_SYSTEM_INSTRUCTION,
)
_CFG_WISDOM_03_SEARCH = genai_types.GenerateContentConfig(
    temperature=0.3,
    system_instruction=WISDOM_SYSTEM_INSTRUCTION,
    tools=[{"google_search": {}}],
)
_CFG_WISDOM_06 = genai_types.GenerateContentConfig(
    temperature=0.6,
    system_instruction=WISDOM_SYSTEM_INSTRUCTION,
)
_CFG_WISDOM_07 = genai_types.GenerateContentConfig(
    temperature=0.7,
    system_instruction=WISDOM_SYSTEM_INSTRUCTION,
)
_CFG_WISDOM_08 = genai_types.GenerateContentConfig(
    temperature=0.8,
    system_instruction=WISDOM_SYSTEM_INSTRUCTION,
)
_CFG_DAILY_JSON = genai_types.GenerateContentConfig(
    temperature=0.8,
    system_instruction=WISDOM_SYSTEM_INSTRUCTION,
    response_mime_type="application/json",
    response_schema=DailyPostSchema,
)

# Регулярные выражения для парсинга мета-тегов (в любом порядке атрибутов property/content)
_OG_TITLE_RE = re.compile(
    r'<meta\s+(?:property=["\']og:title["\']\s+content=["\'](.*?)["\']|content=["\'](.*?)["\']\s+property=["\']og:title["\'])',
    re.IGNORECASE | re.DOTALL,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_OG_DESC_RE = re.compile(
    r'<meta\s+(?:property=["\']og:description["\']\s+content=["\'](.*?)["\']|content=["\'](.*?)["\']\s+property=["\']og:description["\'])',
    re.IGNORECASE | re.DOTALL,
)
_META_DESC_RE = re.compile(
    r'<meta\s+(?:name=["\']description["\']\s+content=["\'](.*?)["\']|content=["\'](.*?)["\']\s+name=["\']description["\'])',
    re.IGNORECASE | re.DOTALL,
)


async def call_gemini_safe(
    contents,
    config: genai_types.GenerateContentConfig | None = None,
    timeout: float = 12.0,
):
    """
    Быстрый и безопасный вызов Gemini API:
    - Жесткий таймаут 12 секунд на запрос (не дает SDK зависать на 1-3 минуты во внутренних ретраях)
    - Мгновенное переключение на следующую модель при 429/503/таймауте
    - Кулдаун 5 минут для перегруженных моделей и запоминание самой быстрой рабочей модели
    """
    global _preferred_model
    now = time.monotonic()

    # Формируем список доступных моделей, ставя проверенную рабочую модель на первое место
    candidates = [
        m for m in _MODELS_PRIORITY
        if m not in _DEAD_MODELS and _COOLDOWN_UNTIL.get(m, 0.0) <= now
    ]
    if not candidates:
        # Если все в кулдауне — сбрасываем кулдауны и пробуем живые модели
        _COOLDOWN_UNTIL.clear()
        candidates = [m for m in _MODELS_PRIORITY if m not in _DEAD_MODELS] or list(_MODELS_PRIORITY)

    if _preferred_model and _preferred_model in candidates:
        candidates.remove(_preferred_model)
        candidates.insert(0, _preferred_model)

    for model in candidates:
        try:
            kwargs = {"model": model, "contents": contents}
            if config:
                kwargs["config"] = config

            response = await asyncio.wait_for(
                gemini.aio.models.generate_content(**kwargs),
                timeout=timeout,
            )
            _preferred_model = model
            return response

        except asyncio.TimeoutError:
            logging.warning(f"Gemini таймаут ({model} > {timeout}с). Переключаемся на следующую модель...")
            _COOLDOWN_UNTIL[model] = time.monotonic() + 180
        except Exception as e:
            err_str = str(e)
            if "404" in err_str or "NOT_FOUND" in err_str:
                logging.warning(f"Модель {model} не найдена (404). Исключаем навсегда.")
                _DEAD_MODELS.add(model)
            elif any(
                code in err_str
                for code in ("429", "RESOURCE_EXHAUSTED", "RESOURCE_EXUSITED", "503", "UNAVAILABLE", "high demand")
            ):
                logging.warning(f"Gemini лимит/перегрузка ({model}). Мгновенно переключаемся на резервную модель...")
                _COOLDOWN_UNTIL[model] = time.monotonic() + 300
            else:
                logging.error(f"Ошибка Gemini ({model}): {e}")
                _COOLDOWN_UNTIL[model] = time.monotonic() + 60

    raise RuntimeError("Сервера Gemini временно недоступны. Попробуйте позже.")


async def ask_wisdom(
    user_question: str,
    user: types.User | None = None,
    reply_quote: str | None = None,
) -> str:
    user_name = user.first_name if user and user.first_name else "Участник"
    username_tag = f"@{user.username}" if user and user.username else user_name
    user_profile = get_user_context(user)
    q_lower = user_question.lower()

    combined_query_check = q_lower + (" " + reply_quote.lower() if reply_quote else "")
    needs_weather = any(word in combined_query_check for word in WEATHER_KEYWORDS)

    # Параллельно запрашиваем историю чата и погоду (если нужно)
    if needs_weather:
        weather_query = (
            user_question
            if any(w in q_lower for w in WEATHER_DIRECT_KEYWORDS)
            else (reply_quote or user_question)
        )
        recent_dialogue, forecast_data = await asyncio.gather(
            get_recent_messages(hours=1, limit=6),
            fetch_dynamic_weather(weather_query, user),
        )
        weather_context = (
            f"\n\n[РЕАЛЬНАЯ ТОЧНАЯ МЕТЕОСВОДКА ИЗ МЕТЕОСЛУЖБЫ]:\n"
            f"{forecast_data}\n"
            f"СТРОГО ОБЯЗАТЕЛЬНО используй эти ТОЧНЫЕ цифры (температуру, ветер, осадки) для ответа! "
            f"Отвечай конкретно по той локации, которая указана в сводке (не приплетай другие города!)."
        )
    else:
        recent_dialogue = await get_recent_messages(hours=1, limit=6)
        weather_context = ""

    reply_context_str = (
        f"\n[ПОЛЬЗОВАТЕЛЬ ОТВЕЧАЕТ НА ЭТУ РЕПЛИКУ]: '{reply_quote}'\n"
        if reply_quote
        else ""
    )

    prompt = (
        f"[КОНТЕКСТ СОБЕСЕДНИКА]: {user_profile}\n\n"
        f"[ПОСЛЕДНИЕ СООБЩЕНИЯ В ЧАТЕ ДЛЯ ПАМЯТИ И КОНТЕКСТА]:\n{recent_dialogue}\n"
        f"{reply_context_str}"
        f"{weather_context}\n\n"
        f'Тебе пишет {user_name} ({username_tag}): "{user_question}"\n'
        "Ответь ему в своём пацанском стиле, строго соблюдая память диалога (понимай о чем шла речь выше, не противоречь себе). "
        "Никакой лести и подхалимажа! Отвечай уверенно, с юмором и строго по реальным фактам (2-3 предложения)."
    )

    try:
        response = await call_gemini_safe(contents=prompt, config=_CFG_WISDOM_06)
        return response.text.strip()
    except Exception as e:
        logging.error(f"Gemini вопрос ошибка: {e}")
        return "Я рядом, но связь чуть сбоит. Выдохни и переспроси через пару секунд."


async def fact_check_dispute(query: str) -> str:
    """Фактчекер споров через Gemini с выходом в поиск."""
    prompt = (
        f"Тебя попросили рассудить спор и найти точный железный факт по вопросу: '{query}'.\n"
        "Ответь кратко (2-3 предложения), с пруфами и железной логикой, по-пацански и объективно."
    )
    try:
        try:
            response = await call_gemini_safe(contents=prompt, config=_CFG_WISDOM_03_SEARCH)
            return response.text.strip()
        except Exception:
            response = await call_gemini_safe(contents=prompt, config=_CFG_WISDOM_03)
            return response.text.strip()
    except Exception as e:
        logging.error(f"Ошибка фактчека: {e}")
        return "Спор мутный, пруфов в эфире не нашёл. Разберитесь на словах."


def _first_group(match: re.Match | None) -> str:
    if not match:
        return ""
    val = next((g for g in match.groups() if g), "")
    return html.unescape(val.strip())


async def fetch_url_metadata(url: str) -> str:
    """Извлекает заголовок и описание страницы по ссылке (читает только первые 64 КБ HTML)."""
    session = await get_http_session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }

    if "youtube.com" in url or "youtu.be" in url:
        try:
            async with session.get(
                "https://www.youtube.com/oembed",
                params={"url": url, "format": "json"},
                timeout=aiohttp.ClientTimeout(total=4),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    title = data.get("title", "")
                    author = data.get("author_name", "")
                    return f"Видео на YouTube: '{title}' от автора '{author}'"
        except Exception:
            pass

    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                raw_chunk = await resp.content.read(65536)
                html_text = raw_chunk.decode("utf-8", errors="ignore")

                title = _first_group(_OG_TITLE_RE.search(html_text)) or _first_group(_TITLE_RE.search(html_text))
                desc = _first_group(_OG_DESC_RE.search(html_text)) or _first_group(_META_DESC_RE.search(html_text))

                if title or desc:
                    return f"Заголовок: {title}\nОписание/Суть: {desc}"
    except Exception as e:
        logging.warning(f"Не удалось вытащить метаданные ссылки ({url}): {e}")

    return ""


async def summarize_media_url(url: str, user: types.User | None = None) -> str:
    """Быстрый пересказ ссылок (YouTube, статьи, посты)."""
    meta_info = await fetch_url_metadata(url)
    user_profile = get_user_context(user)

    if meta_info:
        prompt = (
            f"[КОНТЕКСТ СОБЕСЕДНИКА]: {user_profile}\n"
            f"Пользователь прислал ссылку: {url}\n"
            f"Вот реальные данные о содержании этой страницы/видео:\n{meta_info}\n\n"
            "Сделай краткий, живой пересказ (1-2 ёмких предложения), в чём суть материала, "
            "и вынеси вердикт в своём стиле: стоит ли тратить время или это проходняк."
        )
    else:
        prompt = (
            f"[КОНТЕКСТ СОБЕСЕДНИКА]: {user_profile}\n"
            f"Пользователь прислал ссылку: {url}\n"
            "Попробуй определить суть по ссылке и вынеси краткий вердикт (1-2 предложения)."
        )

    try:
        response = await call_gemini_safe(contents=prompt, config=_CFG_WISDOM_06)
        return response.text.strip()
    except Exception as e:
        logging.error(f"Ошибка пересказа ссылки: {e}")
        return "Ссылка не открывается или закрыта от посторонних глаз."


async def analyze_visual_media(
    media_bytes: bytes,
    mime_type: str,
    caption: str = "",
    user: types.User | None = None,
) -> str:
    """Gemini Vision: анализ фото и кружочков."""
    user_name = user.first_name if user and user.first_name else "Участник"
    user_profile = get_user_context(user)
    prompt = (
        f"[КОНТЕКСТ СОБЕСЕДНИКА]: {user_profile}\n"
        f"Тебе прислал медиа-файл {user_name}. "
        f"Подпись к файлу (если есть): '{caption}'. "
        "Внимательно посмотри на изображение/видео и прокомментируй по-пацански, с юмором, подколом или респектом (2 предложения)."
    )
    try:
        part = genai_types.Part.from_bytes(data=media_bytes, mime_type=mime_type)
        response = await call_gemini_safe(contents=[part, prompt], config=_CFG_WISDOM_07)
        return response.text.strip()
    except Exception as e:
        logging.error(f"Ошибка Vision: {e}")
        return "Глаза замылились, не смог разобрать картинку. Перешли ещё разок."


async def transcribe_voice_bytes(voice_bytes: bytes) -> str:
    """Перевод голосового сообщения в текст через Gemini Audio."""
    prompt = (
        "Расшифруй это голосовое сообщение дословно на русском языке. "
        "Напиши ТОЛЬКО текст того, что сказано, без лишних вступлений."
    )
    try:
        part = genai_types.Part.from_bytes(data=voice_bytes, mime_type="audio/ogg")
        response = await call_gemini_safe(contents=[part, prompt], config=_CFG_AUDIO_02)
        return response.text.strip()
    except Exception as e:
        logging.error(f"Ошибка транскрибации войса: {e}")
        return ""


async def check_ilya_ponto_radar(message: types.Message) -> str:
    """Радар понтов и духоты (только для Ильи @Akishinov)."""
    if not message.from_user:
        return ""
    username = (message.from_user.username or "").lower()
    first_name = (message.from_user.first_name or "").lower()

    if username == "akishinov" or "илья" in first_name:
        text_lower = (message.text or message.caption or "").lower()
        if any(k in text_lower for k in PONT_KEYWORDS):
            score = round(random.uniform(7.5, 9.9), 1)
            prompt = (
                f"Илья (@Akishinov) написал следующее сообщение: '{message.text}'.\n"
                f"Радар понтов зафиксировал уровень {score}/10! Приземли его ироничным, смешным и пацанским подколом "
                "про нефть, баррели, бабло или вахту (строго 1-2 предложения)."
            )
            try:
                response = await call_gemini_safe(contents=prompt, config=_CFG_WISDOM_08)
                return (
                    f"🚨 <b>РАДАР ПОНТОВ:</b> Зафиксирован уровень понтов <b>{score}/10</b>!\n\n"
                    f"🧙 {html.escape(response.text.strip())}"
                )
            except Exception as e:
                logging.error(f"Ошибка радара понтов: {e}")
    return ""


async def generate_text() -> dict[str, str]:
    chat = await get_recent_messages()
    current_date_str = datetime.now(TZ).strftime("%d.%m.%Y")

    prompt = f"""
Каждое утро создавай короткую мысль дня на основе последних разговоров и указывай праздник.

Дата:
{current_date_str}

История сообщений чата (каждая строка записана в формате [Имя Автора]: сообщение):
{chat}

Правила аналитики текста:
- В истории разные участники пишут разное. Обращай внимание, КОМУ принадлежит фраза. НЕ смешивай мысли разных авторов в одну проблему.

Правила для JSON полей (СТРОГО):
- holiday: Напиши исключительно ОФИЦИАЛЬНЫЙ, общеизвестный реальный праздник или памятную дату, которая действительно выпадает на этот день ({current_date_str}) в мире или России. Категорически ЗАПРЕЩЕНО выдумывать шуточные или фейковые праздники на основе контекста разговоров.
- quote: Мудрая очень короткая мысль (1-2 предложения) с лёгким юмором и иронией, которая тонко обыгрывает настроение разговоров.

Дополнительные правила:
- мысль должна быть короткой, живой и вызывать улыбку;
- не использовать имена людей и кошачьих словечек;
- не упоминать Telegram, бота или технологии.
"""
    try:
        response = await call_gemini_safe(contents=prompt, config=_CFG_DAILY_JSON)
        raw_text = response.text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(raw_text)
    except Exception as e:
        logging.error(f"Gemini ошибка: {e}")
        return {
            "holiday": "День спокойных решений",
            "quote": "Даже самый длинный путь начинается с маленького шага, а иногда — с перезапуска утром.",
        }


async def generate_summary() -> str:
    """Генерирует краткую выжимку переписки за последние 12 часов."""
    chat = await get_recent_messages(hours=12, limit=200)
    prompt = f"""
Посмотри переписку друзей за последнее время и сделай очень краткую, емкую выжимку в 3-4 буллитах (главные темы).

Переписка (формат [Имя Автора]: текст):
{chat}

Формат ответа:
• Тезис 1
• Тезис 2
• Тезис 3

Не используй реальные имена. Напиши лаконично и коротко.
"""
    try:
        response = await call_gemini_safe(contents=prompt, config=_CFG_WISDOM_07)
        return response.text.strip()
    except Exception as e:
        logging.error(f"Ошибка создания summary: {e}")
        return "Не удалось разобрать ваши перешёптывания — сервера перегружены."


async def generate_weekly_digest() -> str:
    """Дайджест итогов недели."""
    chat = await get_weekly_sampled_messages()
    prompt = f"""
Настали итоги недели! Проанализируй историю чата за неделю и составь короткий, красивый и смешной дайджест.

История:
{chat}

Составь небольшой пост из 3 частей:
1. 🏆 Главная тема недели
2. 🎭 Атмосфера недели (в 1-2 предложениях)
3. 📜 Напутствие Мяумяуры на следующую неделю (короткий мудрый совет)

Пиши без имён пользователей, с лёгкой иронией, лаконично.
"""
    try:
        response = await call_gemini_safe(contents=prompt, config=_CFG_WISDOM_08)
        return response.text.strip()
    except Exception as e:
        logging.error(f"Ошибка недели: {e}")
        return "Неделя прошла так стремительно, что даже мудрец не успел её осмыслить."


async def maybe_join_conversation(last_user: types.User | None = None) -> str:
    """Анализирует контекст беседы и решает, стоит ли вклиниться в разговор как живой друг."""
    chat = await get_recent_messages(hours=1, limit=8)
    if chat == "В чате было спокойно.":
        return ""

    user_name = last_user.first_name if last_user and last_user.first_name else "Участник"
    username_tag = f"@{last_user.username}" if last_user and last_user.username else user_name

    prompt = f"""
Вот последние сообщения в чате:
{chat}

Ты собираешься отреагировать на реплику пользователя {user_name} ({username_tag}).
1. Вникни в суть разговора и ответь уверенно, по-пацански и без подхалимажа, помня роли каждого в банде (Захар — главный кент, Паша — служит на Севере на Святом Носе в Баренцевом море, Илья — нефтяник для подколов).
2. Если уместно органично подключиться — напиши 1–2 живых предложения.
3. Если разговор сугубо личный, тема не располагает к вмешательству или сказать нечего — напиши строго одно слово: SKIP
"""
    try:
        response = await call_gemini_safe(contents=prompt, config=_CFG_WISDOM_08)
        text = response.text.strip()
        if "SKIP" in text or not text:
            return ""
        return text
    except Exception as e:
        logging.error(f"Ошибка спонтанного комментария: {e}")
        return ""
