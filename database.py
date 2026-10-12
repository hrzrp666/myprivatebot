import asyncio
import logging
from datetime import datetime, timedelta
import aiosqlite

from config import DB_PATH, TZ

_db_conn: aiosqlite.Connection | None = None
_write_lock = asyncio.Lock()


async def init_db() -> None:
    global _db_conn
    _db_conn = await aiosqlite.connect(DB_PATH, timeout=30)

    # Оптимизация производительности SQLite в режиме WAL
    await _db_conn.execute("PRAGMA journal_mode=WAL;")
    await _db_conn.execute("PRAGMA synchronous=NORMAL;")
    await _db_conn.execute("PRAGMA temp_store=MEMORY;")
    await _db_conn.execute("PRAGMA cache_size=-20000;")
    await _db_conn.execute("PRAGMA mmap_size=67108864;")

    await _db_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            text TEXT,
            created TEXT
        )
        """
    )
    await _db_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS posts(
            date TEXT PRIMARY KEY
        )
        """
    )
    await _db_conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_messages_created
        ON messages(created)
        """
    )
    await _db_conn.commit()
    logging.info("SQLite готова (WAL + ускоренные PRAGMA)")


async def close_db() -> None:
    global _db_conn
    if _db_conn is not None:
        await _db_conn.close()
        _db_conn = None


def _trim_history(text: str, max_chars: int = 8000) -> str:
    """Обрезает историю до лимита символов по границе строки."""
    if len(text) <= max_chars:
        return text
    trimmed = text[-max_chars:]
    first_newline = trimmed.find("\n")
    return trimmed[first_newline + 1:] if first_newline != -1 else trimmed


async def save_message(username: str, text: str) -> None:
    if _db_conn is None:
        return
    async with _write_lock:
        await _db_conn.execute(
            "INSERT INTO messages (username, text, created) VALUES (?, ?, ?)",
            (username, text[:1000], datetime.now(TZ).isoformat()),
        )
        await _db_conn.commit()


async def get_recent_messages(hours: int = 24, limit: int = 300) -> str:
    if _db_conn is None:
        return "В чате было спокойно."

    limit_time = (datetime.now(TZ) - timedelta(hours=hours)).isoformat()
    async with _db_conn.execute(
        """
        SELECT username, text
        FROM messages
        WHERE created >= ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit_time, limit),
    ) as cursor:
        rows = await cursor.fetchall()

    if not rows:
        return "В чате было спокойно."

    rows.reverse()
    result = "\n".join(f"[{user}]: {text}" for user, text in rows)
    return _trim_history(result)


async def get_weekly_sampled_messages() -> str:
    """Получает репрезентативную выборку сообщений за 7 дней без потери контекста."""
    if _db_conn is None:
        return "В чате было спокойно."

    limit_time = (datetime.now(TZ) - timedelta(days=7)).isoformat()
    async with _db_conn.execute(
        """
        SELECT username, text, created
        FROM messages
        WHERE created >= ?
        ORDER BY created ASC
        """,
        (limit_time,),
    ) as cursor:
        rows = await cursor.fetchall()

    if not rows:
        return "В чате было спокойно."

    daily_messages: dict[str, list[str]] = {}
    for user, text, created in rows:
        day_key = created[:10]  # YYYY-MM-DD
        daily_messages.setdefault(day_key, []).append(f"[{user}]: {text}")

    result_lines: list[str] = []
    for day_key, msgs in daily_messages.items():
        result_lines.append(f"--- Дата: {day_key} ---")
        step = max(1, len(msgs) // 20)
        result_lines.extend(msgs[::step][:20])

    return _trim_history("\n".join(result_lines))


async def clear_old_messages() -> None:
    if _db_conn is None:
        return
    limit_time = (datetime.now(TZ) - timedelta(days=30)).isoformat()
    async with _write_lock:
        await _db_conn.execute("DELETE FROM messages WHERE created < ?", (limit_time,))
        await _db_conn.commit()
    logging.info("Старая история очищена")


async def was_post_today() -> bool:
    if _db_conn is None:
        return False
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    async with _db_conn.execute("SELECT 1 FROM posts WHERE date = ?", (today,)) as cursor:
        return (await cursor.fetchone()) is not None


async def save_post_today() -> None:
    if _db_conn is None:
        return
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    async with _write_lock:
        await _db_conn.execute("INSERT OR IGNORE INTO posts(date) VALUES (?)", (today,))
        await _db_conn.commit()
