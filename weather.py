import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo
import aiohttp
from aiogram import types

from config import (
    IRKUTSK_LAT,
    IRKUTSK_LON,
    KULTUK_LAT,
    KULTUK_LON,
    TZ,
    TZ_MSK,
    WEATHER_CODES,
)

_http_session: aiohttp.ClientSession | None = None

# Кэш метеоданных: key -> (value, timestamp)
_WEATHER_CACHE: dict[str, tuple[Any, float]] = {}
_MAX_CACHE_SIZE = 100

# Предлог должен стоять отдельным словом: раньше «в» находилось и в конце других слов.
_CITY_RE = re.compile(r"(?<![а-яa-z0-9])(?:в|во|на|по|городе|город)\s+([а-яa-z0-9\-]+)")
_STOP_WORDS = frozenset({
    "погоду", "погоде", "погодка", "погодку", "погодам", "делам", "пацански",
    "факту", "душе", "жизни", "утром", "вечером", "ночью", "день", "дням", "завтра",
    "сегодня", "понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье",
    "чате", "чат", "работе", "работу", "улице", "доме", "дворе", "машине", "жизнь", "целом",
    "общем", "принципе", "итоге", "связи", "сети", "интернете", "теории", "голове",
})
# Явные города: основа слова ищется по границе слова, а не внутри чужих слов
_KULTUK_RE = re.compile(r"(?<![а-я])култук")
_IRKUTSK_RE = re.compile(r"(?<![а-я])иркутск")
_MOSCOW_RE = re.compile(r"(?<![а-я])москв")
_SPB_RE = re.compile(r"(?<![а-я])(?:питер|петербург)")
# Слова, прямо говорящие о погоде: только тогда пробуем вытащить город из фразы
_WEATHER_WORD_RE = re.compile(r"погод|температур|градус|прогноз")


def get_cached_weather(key: str, max_age_seconds: int = 600) -> Any | None:
    entry = _WEATHER_CACHE.get(key)
    if entry and (time.monotonic() - entry[1]) < max_age_seconds:
        return entry[0]
    return None


def set_cached_weather(key: str, val: Any) -> None:
    now = time.monotonic()
    if len(_WEATHER_CACHE) >= _MAX_CACHE_SIZE:
        # Удаляем самые старые записи, чтобы кэш не разрастался при долгом аптайме
        oldest_keys = sorted(_WEATHER_CACHE, key=lambda k: _WEATHER_CACHE[k][1])[:25]
        for k in oldest_keys:
            _WEATHER_CACHE.pop(k, None)
    _WEATHER_CACHE[key] = (val, now)


def get_last_known_weather(key: str) -> Any | None:
    entry = _WEATHER_CACHE.get(key)
    return entry[0] if entry else None


async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        connector = aiohttp.TCPConnector(limit=30, ttl_dns_cache=300)
        _http_session = aiohttp.ClientSession(connector=connector)
    return _http_session


async def close_http_session() -> None:
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
        _http_session = None


_DAY_SLOTS = {"🌅 Утро": 9, "☀️ День": 15, "🌆 Вечер": 21}
NO_DATA = "нет данных"


def _empty_day() -> dict[str, str]:
    return {title: NO_DATA for title in _DAY_SLOTS}


def _has_data(day: dict[str, str] | None) -> bool:
    return bool(day) and any(v != NO_DATA for v in day.values())


def parse_open_meteo_day(data: dict, today_str: str) -> dict[str, str] | None:
    """Утро/день/вечер из ответа Open-Meteo. None — если нужных часов в ответе нет."""
    hourly = data.get("hourly", {}) if isinstance(data, dict) else {}
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    winds = hourly.get("wind_speed_10m", [])
    codes = hourly.get("weather_code", [])
    if not times or not temps:
        return None

    time_to_idx = {t[:16]: i for i, t in enumerate(times)}
    result: dict[str, str] = {}
    for title, hour in _DAY_SLOTS.items():
        idx = time_to_idx.get(f"{today_str}T{hour:02d}:00")
        if idx is not None and idx < len(temps) and temps[idx] is not None:
            t_val = round(temps[idx])
            w_val = round(winds[idx]) if idx < len(winds) and winds[idx] is not None else 0
            c_val = codes[idx] if idx < len(codes) and codes[idx] is not None else 0
            result[title] = f"{t_val}°C, {WEATHER_CODES.get(c_val, 'ясно')}, ветер {w_val} м/с"
        else:
            result[title] = NO_DATA
    return result if _has_data(result) else None


def parse_wttr_day(data: dict) -> dict[str, str] | None:
    """Утро/день/вечер из ответа wttr.in (format=j1): слоты по 3 часа с полем time 900/1500/2100."""
    try:
        hourly = data["weather"][0]["hourly"]
    except (KeyError, IndexError, TypeError):
        return None

    by_time = {str(slot.get("time")): slot for slot in hourly if isinstance(slot, dict)}
    result: dict[str, str] = {}
    for title, hour in _DAY_SLOTS.items():
        slot = by_time.get(str(hour * 100))
        if not slot or slot.get("tempC") in (None, ""):
            result[title] = NO_DATA
            continue
        lang_ru = slot.get("lang_ru") or slot.get("weatherDesc") or [{}]
        desc = (lang_ru[0].get("value") or "ясно").strip().lower()
        try:
            wind = round(float(slot.get("windspeedKmph", 0)) / 3.6)
        except (TypeError, ValueError):
            wind = 0
        result[title] = f"{slot['tempC']}°C, {desc}, ветер {wind} м/с"
    return result if _has_data(result) else None


async def _fetch_open_meteo(lat: float, lon: float) -> dict | None:
    """Сырой ответ Open-Meteo или None (с понятной записью в лог, почему не вышло)."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=temperature_2m,weather_code,wind_speed_10m"
        "&wind_speed_unit=ms&forecast_days=2&timezone=auto"
    )
    headers = {"User-Agent": "TelegramWeatherBot/2.0 (Mozilla/5.0)"}
    session = await get_http_session()
    for attempt in range(2):
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                return await resp.json()
            body = (await resp.text())[:200]
            logging.warning(f"Open-Meteo ответил {resp.status} ({lat}, {lon}): {body}")
            if resp.status != 429:
                return None
        await asyncio.sleep(2 * (attempt + 1))
    return None


async def _fetch_wttr(lat: float, lon: float) -> dict | None:
    """Сырой ответ wttr.in (запасной источник) или None."""
    session = await get_http_session()
    url = f"https://wttr.in/{lat},{lon}?format=j1&lang=ru"
    async with session.get(
        url, headers={"User-Agent": "curl/8.0"}, timeout=aiohttp.ClientTimeout(total=10)
    ) as resp:
        if resp.status != 200:
            logging.warning(f"wttr.in ответил {resp.status} ({lat}, {lon})")
            return None
        return await resp.json(content_type=None)


async def get_weather_day(lat: float, lon: float, tz: ZoneInfo = TZ) -> dict[str, str]:
    """Погода на утро, день и вечер для утреннего поста.

    Сначала Open-Meteo, при отказе — wttr.in. Раньше запасного источника не было:
    когда Open-Meteo отказывал серверу Render (лимит 429 на общий адрес хостинга),
    в посте было «нет данных» во всех строках.
    """
    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    cache_key = f"day:{lat}:{lon}:{today_str}"

    cached = get_cached_weather(cache_key, max_age_seconds=900)
    if cached is not None:
        return cached

    sources = (
        ("Open-Meteo", _fetch_open_meteo, lambda data: parse_open_meteo_day(data, today_str)),
        ("wttr.in", _fetch_wttr, parse_wttr_day),
    )
    for name, fetch, parse in sources:
        try:
            data = await fetch(lat, lon)
            result = parse(data) if data else None
        except Exception as e:
            logging.warning(f"Погода из {name} не получена ({lat}, {lon}): {e}")
            continue
        if result:
            set_cached_weather(cache_key, result)
            return result
        logging.warning(f"{name}: нет данных на сегодня ({lat}, {lon})")

    logging.error(f"Погода недоступна из всех источников ({lat}, {lon})")
    return get_last_known_weather(cache_key) or _empty_day()


def _resolve_location(
    query_text: str, user: types.User | None
) -> tuple[str, str, tuple[float, float, ZoneInfo] | None]:
    """Определяет целевую локацию (ключ запроса, название, координаты + часовой пояс для фоллбэка)."""
    # user оставлен в сигнатуре для совместимости; персональных локаций больше нет
    q_lower = query_text.lower()

    if _KULTUK_RE.search(q_lower):
        return (
            f"{KULTUK_LAT},{KULTUK_LON}",
            "Култук (Иркутская область, Байкал)",
            (KULTUK_LAT, KULTUK_LON, TZ),
        )
    if _IRKUTSK_RE.search(q_lower):
        return (
            f"{IRKUTSK_LAT},{IRKUTSK_LON}",
            "Иркутск",
            (IRKUTSK_LAT, IRKUTSK_LON, TZ),
        )
    if _MOSCOW_RE.search(q_lower):
        return "Москва", "Москва", (55.7558, 37.6173, TZ_MSK)
    if _SPB_RE.search(q_lower):
        return "Санкт-Петербург", "Санкт-Петербург", (59.9343, 30.3351, TZ_MSK)

    # Произвольный город берём только если человек прямо спрашивает про погоду
    if _WEATHER_WORD_RE.search(q_lower):
        match = _CITY_RE.search(q_lower)
        if match:
            extracted = match.group(1).strip()
            if extracted not in _STOP_WORDS and len(extracted) > 2:
                return extracted, extracted.capitalize(), None

    return (
        f"{KULTUK_LAT},{KULTUK_LON}",
        "Култук (Байкал)",
        (KULTUK_LAT, KULTUK_LON, TZ),
    )


def _extract_slot(hourly: list[dict], idx: int) -> tuple[str, str, int | str]:
    if len(hourly) <= idx:
        return "?", "ясно", "?"
    slot = hourly[idx]
    temp = slot.get("tempC", "?")
    lang_ru = slot.get("lang_ru")
    desc = lang_ru[0].get("value", "ясно") if lang_ru else "ясно"
    wind = round(float(slot.get("windspeedKmph", 0)) / 3.6)
    return temp, desc, wind


async def fetch_dynamic_weather(query_text: str, user: types.User | None = None) -> str:
    """
    Находит локацию из вопроса пользователя и достает точный прогноз погоды (с кэшированием и фоллбэком).
    """
    target_loc, loc_name, coords_tz = _resolve_location(query_text, user)
    cache_key = f"dyn:{target_loc}"

    cached = get_cached_weather(cache_key, max_age_seconds=600)
    if cached:
        return cached

    session = await get_http_session()
    headers = {"User-Agent": "Mozilla/5.0"}
    encoded_loc = quote(target_loc, safe=",")
    # lang=ru обязателен: без него wttr.in не отдаёт описание по-русски и бот всегда писал «ясно»
    url_wttr = f"https://wttr.in/{encoded_loc}?format=j1&lang=ru"

    try:
        async with session.get(url_wttr, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                cur = data.get("current_condition", [{}])[0]
                cur_temp = cur.get("temp_C", "?")
                cur_wind = round(float(cur.get("windspeedKmph", 0)) / 3.6)
                cur_desc = cur.get("lang_ru", [{}])[0].get("value", "ясно") if cur.get("lang_ru") else "ясно"

                lines = [
                    f"Локация: {loc_name}",
                    f"Сейчас: {cur_temp}°C, {cur_desc}, ветер {cur_wind} м/с",
                ]

                for i, wday in enumerate(data.get("weather", [])[:2]):
                    date_label = "Сегодня" if i == 0 else "Завтра"
                    max_temp = wday.get("maxtempC", "?")
                    min_temp = wday.get("mintempC", "?")
                    hourly = wday.get("hourly", [])

                    m_temp, m_desc, m_wind = _extract_slot(hourly, 3)
                    d_temp, d_desc, d_wind = _extract_slot(hourly, 5)
                    e_temp, e_desc, e_wind = _extract_slot(hourly, 7)

                    lines.append(
                        f"• {date_label} ({wday.get('date')}): "
                        f"Днем максимум {max_temp}°C, ночью минимум {min_temp}°C. "
                        f"Утро: {m_temp}°C ({m_desc}, ветер {m_wind} м/с). "
                        f"День: {d_temp}°C ({d_desc}, ветер {d_wind} м/с). "
                        f"Вечер: {e_temp}°C ({e_desc}, ветер {e_wind} м/с)."
                    )

                result_str = "\n".join(lines)
                set_cached_weather(cache_key, result_str)
                return result_str
    except Exception as e:
        logging.warning(f"Ошибка запроса погоды wttr.in ({target_loc}): {e}")

    # Фоллбэк 1: последние известные данные из кэша
    last_known = get_last_known_weather(cache_key)
    if last_known:
        return last_known

    # Фоллбэк 2: Open-Meteo по координатам, если wttr.in недоступен
    if coords_tz:
        lat, lon, loc_tz = coords_tz
        day_data = await get_weather_day(lat, lon, tz=loc_tz)
        if any(v != "нет данных" for v in day_data.values()):
            fallback_str = (
                f"Локация: {loc_name}\n"
                f"Сегодня — Утро: {day_data['🌅 Утро']}; "
                f"День: {day_data['☀️ День']}; "
                f"Вечер: {day_data['🌆 Вечер']}."
            )
            set_cached_weather(cache_key, fallback_str)
            return fallback_str

    return f"Локация: {loc_name}. (Данные временно недоступны)."
