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
    KULTUK_LAT,
    KULTUK_LON,
    SVYATOY_NOS_LAT,
    SVYATOY_NOS_LON,
    TZ,
    TZ_MSK,
    WEATHER_CODES,
)

_http_session: aiohttp.ClientSession | None = None

# Кэш метеоданных: key -> (value, timestamp)
_WEATHER_CACHE: dict[str, tuple[Any, float]] = {}
_MAX_CACHE_SIZE = 100

_CITY_RE = re.compile(r"(?:в|во|на|по|городе|город)\s+([а-яА-Яa-zA-Z0-9\-]+)")
_STOP_WORDS = frozenset({
    "погоду", "погоде", "погодка", "погодку", "погодам", "делам", "пацански",
    "факту", "душе", "жизни", "утром", "вечером", "ночью", "день", "дням", "завтра",
    "сегодня", "понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье",
})
_SVYATOY_NOS_KEYS = ("свято", "нос", "баренцев", "мурманск", "север")
_ARMY_KEYS = ("вахт", "дежур", "армей", "служб", "сводк", "рапорт")


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


async def get_weather_day(lat: float, lon: float, tz: ZoneInfo = TZ) -> dict[str, str]:
    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    cache_key = f"day:{lat}:{lon}:{today_str}"

    cached = get_cached_weather(cache_key, max_age_seconds=900)
    if cached is not None:
        return cached

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=temperature_2m,weather_code,wind_speed_10m"
        "&wind_speed_unit=ms"
        "&forecast_days=2"
        "&timezone=auto"
    )
    fallback = {
        "🌅 Утро": "нет данных",
        "☀️ День": "нет данных",
        "🌆 Вечер": "нет данных",
    }
    headers = {"User-Agent": "TelegramWeatherBot/2.0 (Mozilla/5.0)"}

    try:
        session = await get_http_session()
        data = None
        for attempt in range(3):
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    break
                if resp.status == 429:
                    wait_sec = (attempt + 1) * 2
                    logging.warning(f"Open-Meteo 429 (лимит). Ждем {wait_sec} сек...")
                    await asyncio.sleep(wait_sec)
                else:
                    logging.error(f"Open-Meteo error status ({lat}, {lon}): {resp.status}")
                    return get_last_known_weather(cache_key) or fallback

        if not data:
            return get_last_known_weather(cache_key) or fallback

        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        winds = hourly.get("wind_speed_10m", [])
        codes = hourly.get("weather_code", [])

        if not times or not temps:
            return get_last_known_weather(cache_key) or fallback

        # Индекс времени для O(1) поиска вместо линейного перебора
        time_to_idx = {t[:16]: i for i, t in enumerate(times)}
        target_hours = {"🌅 Утро": 9, "☀️ День": 15, "🌆 Вечер": 21}

        result: dict[str, str] = {}
        for title, hour in target_hours.items():
            key_time = f"{today_str}T{hour:02d}:00"
            idx = time_to_idx.get(key_time)
            if idx is not None and idx < len(temps) and temps[idx] is not None:
                t_val = round(temps[idx])
                w_val = round(winds[idx]) if idx < len(winds) and winds[idx] is not None else 0
                c_val = codes[idx] if idx < len(codes) and codes[idx] is not None else 0
                weather_desc = WEATHER_CODES.get(c_val, "ясно")
                result[title] = f"{t_val}°C, {weather_desc}, ветер {w_val} м/с"
            else:
                result[title] = "нет данных"

        set_cached_weather(cache_key, result)
        return result

    except Exception as e:
        logging.error(f"Ошибка получения погоды ({lat}, {lon}): {e}")
        return get_last_known_weather(cache_key) or fallback


def _resolve_location(
    query_text: str, user: types.User | None
) -> tuple[str, str, tuple[float, float, ZoneInfo] | None]:
    """Определяет целевую локацию (ключ запроса, название, координаты + часовой пояс для фоллбэка)."""
    q_lower = query_text.lower()
    username = (user.username or "").lower() if user else ""
    first_name = (user.first_name or "").lower() if user else ""

    if any(k in q_lower for k in _SVYATOY_NOS_KEYS):
        return (
            f"{SVYATOY_NOS_LAT},{SVYATOY_NOS_LON}",
            "Мыс Святой Нос (Баренцево море, Мурманская область)",
            (SVYATOY_NOS_LAT, SVYATOY_NOS_LON, TZ_MSK),
        )
    if "култук" in q_lower:
        return (
            f"{KULTUK_LAT},{KULTUK_LON}",
            "Култук (Иркутская область, Байкал)",
            (KULTUK_LAT, KULTUK_LON, TZ),
        )
    if "иркутск" in q_lower:
        return "Иркутск", "Иркутск", (52.2869, 104.3050, TZ)
    if "москв" in q_lower:
        return "Москва", "Москва", (55.7558, 37.6173, TZ_MSK)
    if "питер" in q_lower or "петербург" in q_lower:
        return "Санкт-Петербург", "Санкт-Петербург", (59.9343, 30.3351, TZ_MSK)

    if username == "tombarket" or first_name in ("павел", "паша") or any(k in q_lower for k in _ARMY_KEYS):
        return (
            f"{SVYATOY_NOS_LAT},{SVYATOY_NOS_LON}",
            "Мыс Святой Нос (Баренцево море, Мурманская область)",
            (SVYATOY_NOS_LAT, SVYATOY_NOS_LON, TZ_MSK),
        )

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
    url_wttr = f"https://wttr.in/{encoded_loc}?format=j1"

    try:
        async with session.get(url_wttr, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
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
