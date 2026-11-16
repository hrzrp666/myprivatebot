import asyncio
import logging
from datetime import datetime, timedelta
import aiosqlite

from config import BOT_NAME, DATABASE_URL, DB_PATH, TZ

# Два хранилища с одним интерфейсом:
#  - Postgres (asyncpg), если задан DATABASE_URL — данные переживают деплои на Render;
#  - SQLite-файл DB_PATH — запасной вариант для локального запуска.
_pool = None  # asyncpg.Pool | None
_db_conn: aiosqlite.Connection | None = None
_write_lock = asyncio.Lock()


async def init_db() -> None:
    global _pool, _db_conn

    if DATABASE_URL:
        import asyncpg  # импорт здесь, чтобы локальный SQLite-режим не требовал asyncpg

        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        async with _pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages(
                    id BIGSERIAL PRIMARY KEY,
                    username TEXT,
                    text TEXT,
                    created TIMESTAMPTZ NOT NULL
                )
                """
            )
            await conn.execute("CREATE TABLE IF NOT EXISTS posts(date TEXT PRIMARY KEY)")
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kv(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated TIMESTAMPTZ NOT NULL
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created)"
            )
        logging.info("Postgres готов (история сохраняется между деплоями)")
        return

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
        CREATE TABLE IF NOT EXISTS kv(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated TEXT NOT NULL
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
    logging.warning(
        "SQLite готова (WAL). На Render без постоянного диска файл стирается при деплое — "
        "задайте DATABASE_URL, чтобы использовать Postgres"
    )


async def close_db() -> None:
    global _pool, _db_conn
    if _pool is not None:
        await _pool.close()
        _pool = None
    if _db_conn is not None:
        await _db_conn.close()
        _db_conn = None


def _is_ready() -> bool:
    return _pool is not None or _db_conn is not None


def _trim_history(text: str, max_chars: int = 8000) -> str:
    """Обрезает историю до лимита символов по границе строки."""
    if len(text) <= max_chars:
        return text
    trimmed = text[-max_chars:]
    first_newline = trimmed.find("\n")
    return trimmed[first_newline + 1:] if first_newline != -1 else trimmed


async def save_message(username: str, text: str) -> None:
    if not _is_ready():
        return
    now = datetime.now(TZ)
    if _pool is not None:
        await _pool.execute(
            "INSERT INTO messages (username, text, created) VALUES ($1, $2, $3)",
            username,
            text[:1000],
            now,
        )
        return
    async with _write_lock:
        await _db_conn.execute(
            "INSERT INTO messages (username, text, created) VALUES (?, ?, ?)",
            (username, text[:1000], now.isoformat()),
        )
        await _db_conn.commit()


async def get_recent_rows(hours: int = 24, limit: int = 60) -> list[tuple[str, str, datetime]]:
    """Последние сообщения как (имя, текст, время) от старых к новым — для диалога с моделью."""
    if not _is_ready():
        return []

    limit_dt = datetime.now(TZ) - timedelta(hours=hours)
    if _pool is not None:
        records = await _pool.fetch(
            """
            SELECT username, text, created
            FROM messages
            WHERE created >= $1
            ORDER BY id DESC
            LIMIT $2
            """,
            limit_dt,
            limit,
        )
        rows = [(r["username"], r["text"], r["created"]) for r in records]
    else:
        async with _db_conn.execute(
            """
            SELECT username, text, created
            FROM messages
            WHERE created >= ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit_dt.isoformat(), limit),
        ) as cursor:
            raw_rows = await cursor.fetchall()
        rows = [(u, t, datetime.fromisoformat(c)) for u, t, c in raw_rows]

    rows.reverse()
    return rows


async def get_recent_messages(
    hours: int = 24,
    limit: int = 300,
    with_time: bool = False,
    max_chars: int = 8000,
) -> str:
    """История чата строками «[Имя]: текст» от старых к новым.

    with_time=True добавляет метку «[дд.мм чч:мм]», чтобы модель понимала, когда что было сказано.
    """
    rows = await get_recent_rows(hours=hours, limit=limit)
    if not rows:
        return "В чате было спокойно."

    if with_time:
        lines = [
            f"[{created.astimezone(TZ).strftime('%d.%m %H:%M')}] [{user}]: {text}"
            for user, text, created in rows
        ]
    else:
        lines = [f"[{user}]: {text}" for user, text, _ in rows]
    return _trim_history("\n".join(lines), max_chars=max_chars)


async def get_weekly_sampled_messages() -> str:
    """Получает репрезентативную выборку сообщений за 7 дней без потери контекста."""
    if not _is_ready():
        return "В чате было спокойно."

    limit_dt = datetime.now(TZ) - timedelta(days=7)
    if _pool is not None:
        records = await _pool.fetch(
            """
            SELECT username, text, created
            FROM messages
            WHERE created >= $1
            ORDER BY created ASC
            """,
            limit_dt,
        )
        # created приходит как datetime с часовым поясом — приводим к иркутскому дню
        rows = [
            (r["username"], r["text"], r["created"].astimezone(TZ).strftime("%Y-%m-%d"))
            for r in records
        ]
    else:
        async with _db_conn.execute(
            """
            SELECT username, text, created
            FROM messages
            WHERE created >= ?
            ORDER BY created ASC
            """,
            (limit_dt.isoformat(),),
        ) as cursor:
            raw_rows = await cursor.fetchall()
        rows = [(user, text, created[:10]) for user, text, created in raw_rows]  # YYYY-MM-DD

    if not rows:
        return "В чате было спокойно."

    daily_messages: dict[str, list[str]] = {}
    for user, text, day_key in rows:
        daily_messages.setdefault(day_key, []).append(f"[{user}]: {text}")

    result_lines: list[str] = []
    for day_key, msgs in daily_messages.items():
        result_lines.append(f"--- Дата: {day_key} ---")
        step = max(1, len(msgs) // 20)
        result_lines.extend(msgs[::step][:20])

    return _trim_history("\n".join(result_lines))


async def clear_old_messages() -> None:
    if not _is_ready():
        return
    limit_dt = datetime.now(TZ) - timedelta(days=30)
    if _pool is not None:
        await _pool.execute("DELETE FROM messages WHERE created < $1", limit_dt)
    else:
        async with _write_lock:
            await _db_conn.execute(
                "DELETE FROM messages WHERE created < ?", (limit_dt.isoformat(),)
            )
            await _db_conn.commit()
    logging.info("Старая история очищена")


async def get_user_message_counts(days: int = 7) -> list[tuple[str, int]]:
    """Сколько сообщений написал каждый участник за days дней (без бота), по убыванию."""
    if not _is_ready():
        return []
    limit_dt = datetime.now(TZ) - timedelta(days=days)
    sql = """
        SELECT username, COUNT(*) AS cnt
        FROM messages
        WHERE created >= {p1} AND username <> {p2}
        GROUP BY username
        ORDER BY cnt DESC, username ASC
    """
    if _pool is not None:
        records = await _pool.fetch(sql.format(p1="$1", p2="$2"), limit_dt, BOT_NAME)
        return [(r["username"], int(r["cnt"])) for r in records]
    async with _db_conn.execute(
        sql.format(p1="?", p2="?"), (limit_dt.isoformat(), BOT_NAME)
    ) as cursor:
        return [(u, int(c)) for u, c in await cursor.fetchall()]


async def get_random_quote(
    days: int = 30, min_len: int = 25, max_len: int = 300
) -> tuple[str, str, datetime] | None:
    """Случайная содержательная фраза участника (не бота, без служебных пометок и ссылок)."""
    if not _is_ready():
        return None
    limit_dt = datetime.now(TZ) - timedelta(days=days)
    sql = """
        SELECT username, text, created
        FROM messages
        WHERE created >= {p1} AND username <> {p2}
          AND length(text) BETWEEN {p3} AND {p4}
          AND text NOT LIKE '[%' AND text NOT LIKE '%http%'
        ORDER BY RANDOM()
        LIMIT 1
    """
    if _pool is not None:
        record = await _pool.fetchrow(
            sql.format(p1="$1", p2="$2", p3="$3", p4="$4"), limit_dt, BOT_NAME, min_len, max_len
        )
        return (record["username"], record["text"], record["created"]) if record else None
    async with _db_conn.execute(
        sql.format(p1="?", p2="?", p3="?", p4="?"),
        (limit_dt.isoformat(), BOT_NAME, min_len, max_len),
    ) as cursor:
        row = await cursor.fetchone()
    return (row[0], row[1], datetime.fromisoformat(row[2])) if row else None


async def get_kv(key: str) -> str:
    """Читает значение из хранилища «ключ — текст» (долгая память бота). Нет значения — пустая строка."""
    if not _is_ready():
        return ""
    if _pool is not None:
        value = await _pool.fetchval("SELECT value FROM kv WHERE key = $1", key)
        return value or ""
    async with _db_conn.execute("SELECT value FROM kv WHERE key = ?", (key,)) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else ""


async def set_kv(key: str, value: str) -> None:
    """Записывает значение по ключу (перезаписывает старое)."""
    if not _is_ready():
        return
    now = datetime.now(TZ)
    if _pool is not None:
        await _pool.execute(
            """
            INSERT INTO kv(key, value, updated) VALUES ($1, $2, $3)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated = EXCLUDED.updated
            """,
            key,
            value,
            now,
        )
        return
    async with _write_lock:
        await _db_conn.execute(
            """
            INSERT INTO kv(key, value, updated) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated = excluded.updated
            """,
            (key, value, now.isoformat()),
        )
        await _db_conn.commit()


async def was_post_today() -> bool:
    if not _is_ready():
        return False
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    if _pool is not None:
        return (await _pool.fetchval("SELECT 1 FROM posts WHERE date = $1", today)) is not None
    async with _db_conn.execute("SELECT 1 FROM posts WHERE date = ?", (today,)) as cursor:
        return (await cursor.fetchone()) is not None


async def save_post_today() -> None:
    if not _is_ready():
        return
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    if _pool is not None:
        await _pool.execute("INSERT INTO posts(date) VALUES ($1) ON CONFLICT DO NOTHING", today)
        return
    async with _write_lock:
        await _db_conn.execute("INSERT OR IGNORE INTO posts(date) VALUES (?)", (today,))
        await _db_conn.commit()
