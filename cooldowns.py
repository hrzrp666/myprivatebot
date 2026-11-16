"""Паузы между действиями бота, которые переживают перезапуск (хранятся в базе).

Раньше они жили в памяти процесса: после каждого деплоя бот мог сразу вклиниться,
поставить реакцию или выстрелить радаром понтов, хотя только что это делал.
"""
import asyncio
import json
from datetime import datetime, timedelta

from config import TZ
from database import get_kv, set_kv

_KEY = "cooldowns"
_cache: dict[str, str] | None = None
_lock = asyncio.Lock()


async def _load() -> dict[str, str]:
    global _cache
    if _cache is None:
        raw = await get_kv(_KEY)
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        _cache = data if isinstance(data, dict) else {}
    return _cache


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def since(name: str) -> timedelta | None:
    """Сколько прошло с последнего действия name. None — ещё ни разу."""
    last = _parse((await _load()).get(name))
    return None if last is None else datetime.now(TZ) - last


async def try_acquire(name: str, gap: timedelta) -> bool:
    """Если с прошлого раза прошло не меньше gap — отмечает действие и возвращает True."""
    async with _lock:
        data = await _load()
        now = datetime.now(TZ)
        last = _parse(data.get(name))
        if last is not None and now - last < gap:
            return False
        data[name] = now.isoformat()
        await set_kv(_KEY, json.dumps(data))
        return True


def reset_cache() -> None:
    """Сбрасывает кэш в памяти (для тестов: проверка, что паузы читаются из базы)."""
    global _cache
    _cache = None
