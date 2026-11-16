"""Коллекция стикеров: бот запоминает стикеры, которые кидают в чат, и потом отвечает ими сам."""
import asyncio
import json
import random

from database import get_kv, set_kv

STICKERS_KEY = "stickers"
_MAX_STICKERS = 300
# Два стикера подряд не должны затереть друг друга при одновременной записи
_lock = asyncio.Lock()


async def _load() -> list[dict]:
    raw = await get_kv(STICKERS_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return [s for s in data if isinstance(s, dict) and s.get("id")]


async def remember_sticker(file_id: str, unique_id: str, emoji: str | None) -> None:
    """Сохраняет стикер (без дублей); старые вытесняются, когда коллекция переполнена."""
    async with _lock:
        stickers = await _load()
        if any(s.get("uid") == unique_id for s in stickers):
            return
        stickers.append({"id": file_id, "uid": unique_id, "emoji": emoji or ""})
        await set_kv(STICKERS_KEY, json.dumps(stickers[-_MAX_STICKERS:], ensure_ascii=False))


async def pick_sticker(emoji: str | None = None) -> str | None:
    """file_id стикера с этим эмодзи (или случайного, если эмодзи не задан). None — подходящего нет."""
    stickers = await _load()
    if not stickers:
        return None
    if not emoji:
        return random.choice(stickers)["id"]
    emoji = emoji.replace("️", "")
    matching = [s for s in stickers if s.get("emoji", "").replace("️", "") == emoji]
    return random.choice(matching)["id"] if matching else None


async def available_sticker_emojis() -> list[str]:
    """Эмодзи, для которых в коллекции есть стикеры."""
    seen: list[str] = []
    for s in await _load():
        e = s.get("emoji", "")
        if e and e not in seen:
            seen.append(e)
    return seen
