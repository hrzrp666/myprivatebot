"""Игра «Кто это сказал?»: бот достаёт старую фразу из чата, все угадывают автора."""
import asyncio
import json
import random
import secrets
from dataclasses import dataclass
from datetime import datetime

from database import get_kv, set_kv

_SCORES_KEY = "who_said_scores"
_MAX_GAMES = 50
_scores_lock = asyncio.Lock()


@dataclass
class WhoSaidGame:
    answer: str
    options: list[str]
    said_at: datetime
    solved: bool = False


_games: dict[str, WhoSaidGame] = {}


def build_options(answer: str, names: list[str], rnd: random.Random | None = None) -> list[str]:
    """Варианты ответа: правильный автор + до трёх других участников, в случайном порядке."""
    rnd = rnd or random.Random()
    others = [n for n in dict.fromkeys(names) if n != answer]
    options = [answer] + rnd.sample(others, k=min(3, len(others)))
    rnd.shuffle(options)
    return options


def new_game(answer: str, options: list[str], said_at: datetime) -> str:
    """Регистрирует игру и возвращает её короткий id для кнопок."""
    while len(_games) >= _MAX_GAMES:
        _games.pop(next(iter(_games)))
    game_id = secrets.token_hex(4)
    _games[game_id] = WhoSaidGame(answer=answer, options=options, said_at=said_at)
    return game_id


def get_game(game_id: str) -> WhoSaidGame | None:
    return _games.get(game_id)


async def _load_scores() -> dict[str, int]:
    raw = await get_kv(_SCORES_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else {}


async def add_point(player: str) -> int:
    """+1 очко игроку, возвращает его новый счёт."""
    async with _scores_lock:
        scores = await _load_scores()
        scores[player] = scores.get(player, 0) + 1
        await set_kv(_SCORES_KEY, json.dumps(scores, ensure_ascii=False))
        return scores[player]


async def leaderboard(limit: int = 5) -> list[tuple[str, int]]:
    scores = await _load_scores()
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
