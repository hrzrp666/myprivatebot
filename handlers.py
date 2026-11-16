import asyncio
import html
import io
import logging
import re
from datetime import datetime, timedelta
from aiogram import Bot, F, Router, types
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
)
from google.genai import types as genai_types

import cooldowns
import whosaid
from ai_service import (
    add_manual_fact,
    check_ilya_ponto_radar,
    clear_memory,
    fact_check_dispute,
    generate_summary,
    get_memory_context,
    transcribe_voice_bytes,
    update_long_memory,
)
from brain import BotReply, ask_wisdom, decide_engagement
from config import ADMIN_ID, BOT_NAME, CHAT_ID, TZ, VISION_TRIGGER_KEYWORDS
from database import get_random_quote, get_user_message_counts, save_message
from mood import today_mood
from scheduler import create_daily_post
from stickers import remember_sticker
from tts import text_to_speech_bytes

router = Router()

_bot_info: types.User | None = None
_bot_info_lock = asyncio.Lock()

# Ограничители, чтобы бот не превратился в спамера и не жёг лимит Gemini.
# Паузы для реплик и реакций хранятся в базе и переживают перезапуск (cooldowns.py).
_DECISION_GAP = timedelta(seconds=15)     # не чаще одного решения «реагировать ли» в 15 с
_SPONTANEOUS_GAP = timedelta(minutes=10)  # сам вклинивается не чаще раза в 10 минут
_REACTION_GAP = timedelta(seconds=90)     # сам ставит реакции не чаще раза в полторы минуты
_last_decision_time = datetime.min.replace(tzinfo=TZ)


async def get_bot_info(bot: Bot) -> types.User:
    """Кэширует информацию о боте (с защитой от гонки при старте), чтобы не вызывать bot.get_me() повторно."""
    global _bot_info
    if _bot_info is None:
        async with _bot_info_lock:
            if _bot_info is None:
                _bot_info = await bot.get_me()
    return _bot_info


def _sender_name(message: types.Message) -> str:
    if message.from_user and message.from_user.first_name:
        return message.from_user.first_name
    return "Участник"


def _format_reply_quote(reply: types.Message | None, bot_info: types.User) -> str | None:
    """Текст сообщения, на которое отвечают, с указанием автора (иначе модель путает, кто что сказал)."""
    if not reply:
        return None
    text = reply.text or reply.caption
    if not text:
        return None
    if reply.from_user and reply.from_user.id == bot_info.id:
        author = BOT_NAME
    elif reply.from_user and reply.from_user.first_name:
        author = reply.from_user.first_name
    else:
        author = "Участник"
    return f"[{author}]: {text[:500]}"


# Явное обращение — отвечаем без раздумий. Слово «бот» и косвенные обращения
# решает лёгкая модель (раньше любое «бот» в тексте вызывало ответ невпопад).
_BOT_NAME_TRIGGERS = ("мяумяур", "мудрец")
_BOT_WORD_RE = re.compile(r"(?<![а-яa-z])бот(?![а-яa-z])")


def _is_addressed_to_bot(
    message: types.Message,
    bot_info: types.User,
    text_lower: str,
    extra_keywords: tuple[str, ...] = (),
) -> bool:
    is_reply = bool(
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_info.id
    )
    if is_reply or any(t in text_lower for t in _BOT_NAME_TRIGGERS):
        return True

    bot_username = (bot_info.username or "").lower()
    if bot_username and f"@{bot_username}" in text_lower:
        return True

    if extra_keywords and any(w in text_lower for w in extra_keywords):
        return True

    return False


async def send_bot_reply(message: types.Message, bot: Bot, reply: BotReply) -> None:
    """Отправляет всё, что решила модель: реакцию, текст или голосовое, стикер."""
    if reply.reaction:
        try:
            await message.react([ReactionTypeEmoji(emoji=reply.reaction)])
        except Exception as e:
            logging.warning(f"Реакция не поставилась: {e}")

    if reply.text:
        text = reply.text[:3900]
        await save_message(BOT_NAME, text)
        sent_as_voice = False
        if reply.voice:
            await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.RECORD_VOICE)
            audio_bytes = await text_to_speech_bytes(text)
            if audio_bytes:
                voice_file = BufferedInputFile(audio_bytes, filename="wisdom.mp3")
                await message.reply_voice(voice=voice_file, caption=f"🧙 {html.escape(text[:900])}")
                sent_as_voice = True
        if not sent_as_voice:
            await message.reply(f"🧙 {html.escape(text)}")

    if reply.sticker_file_id:
        try:
            await message.reply_sticker(reply.sticker_file_id)
            if not reply.text:
                await save_message(BOT_NAME, "[стикер]")
        except Exception as e:
            logging.warning(f"Стикер не отправился: {e}")


async def _answer(
    message: types.Message, bot: Bot, text: str, bot_info: types.User, spontaneous: bool = False
) -> None:
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    reply_quote = _format_reply_quote(message.reply_to_message, bot_info)
    reply = await ask_wisdom(text, message.from_user, reply_quote, spontaneous=spontaneous)
    if not reply.is_empty:
        await send_bot_reply(message, bot, reply)


async def respond_to_text(message: types.Message, bot: Bot, text: str) -> None:
    """Общая логика для текста и расшифрованных голосовых: ответить, поставить реакцию или промолчать."""
    global _last_decision_time

    bot_info = await get_bot_info(bot)
    text_lower = text.lower()
    if _is_addressed_to_bot(message, bot_info, text_lower):
        await _answer(message, bot, text, bot_info)
        return

    # Радар понтов — только когда к боту не обращались явно, иначе он перебивал ответ
    ponto_alert = await check_ilya_ponto_radar(message)
    if ponto_alert:
        await message.reply(ponto_alert)
        return

    now = datetime.now(TZ)
    mentions_bot = bool(_BOT_WORD_RE.search(text_lower))
    if not mentions_bot and now - _last_decision_time < _DECISION_GAP:
        return
    _last_decision_time = now

    since_spoke = await cooldowns.since("spontaneous")
    decision = await decide_engagement(
        None if since_spoke is None else since_spoke.total_seconds() / 60
    )

    if decision.action == "reply":
        if decision.addressed:
            await _answer(message, bot, text, bot_info)
        elif await cooldowns.try_acquire("spontaneous", _SPONTANEOUS_GAP):
            await _answer(message, bot, text, bot_info, spontaneous=True)
    elif decision.action == "react" and await cooldowns.try_acquire("reaction", _REACTION_GAP):
        try:
            await message.react([ReactionTypeEmoji(emoji=decision.emoji)])
        except Exception as e:
            logging.warning(f"Реакция не поставилась: {e}")


# =====================================================
# COMMANDS
# =====================================================

@router.message(Command("start"))
async def start_cmd(message: types.Message) -> None:
    await message.answer(
        "🧙 Мудрец Мяумяура запущен.\n\n"
        "📜 <b>Мои возможности:</b>\n"
        "• Каждое утро в 09:00 — Мысль дня и погода\n"
        "• /summary — Краткая выжимка (что пропустили в чате)\n"
        "• /пробей &lt;вопрос&gt; — Разрулить любой спор по фактам с пруфами\n"
        "• /memory — Что я помню о вас, /remember &lt;факт&gt; — запомнить вручную\n"
        "• Каждое воскресенье в 20:00 — Итоги недели\n"
        "• Кидай голосовые (я расшифрую) и фотки/кружочки (я заценю)!\n"
        "• Зови «Мяумяура …»: сам гляну погоду, поищу в интернете, открою ссылку, запомню важное, "
        "отвечу голосом или стикером. Иногда сам вклиниваюсь и ставлю реакции.\n"
        "• /who — игра «Кто это сказал?», /score — очки\n"
        "• /mood — моё настроение дня (каждый день новый образ)"
    )


def _is_admin(message: types.Message) -> bool:
    return bool(ADMIN_ID and message.from_user and str(message.from_user.id) == str(ADMIN_ID))


@router.message(Command("memory", "память"))
async def memory_cmd(message: types.Message) -> None:
    """Показывает, что бот помнит о компании (долгая память)."""
    if message.chat.id != CHAT_ID:
        return
    memory = await get_memory_context()
    if not memory:
        await message.reply(
            "🧠 Долгая память пока пустая. Я обновляю её раз в сутки по вашей переписке, "
            "а факт можно добавить сразу: <code>/remember Захар купил машину</code>"
        )
        return
    await message.reply(f"🧠 <b>Что я помню о вас:</b>\n\n{html.escape(memory[:3300])}")


@router.message(Command("remember", "запомни"))
async def remember_cmd(message: types.Message) -> None:
    """Добавляет факт в долгую память вручную (можно ответом на сообщение)."""
    if message.chat.id != CHAT_ID:
        return
    parts = (message.text or "").split(maxsplit=1)
    fact = parts[1].strip() if len(parts) > 1 else ""
    if not fact and message.reply_to_message:
        fact = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
    if not fact:
        await message.reply("Напиши, что запомнить: <code>/remember Илья вернулся с вахты</code>")
        return
    await add_manual_fact(fact)
    await message.reply("🧠 Запомнил.")


@router.message(Command("forget", "забудь"))
async def forget_cmd(message: types.Message) -> None:
    """Полностью стирает долгую память (только администратор)."""
    if not _is_admin(message):
        return
    await clear_memory()
    await message.reply("🧠 Долгая память очищена.")


@router.message(Command("memupdate"))
async def memupdate_cmd(message: types.Message) -> None:
    """Принудительно обновляет долгую память по переписке за сутки (только администратор)."""
    if not _is_admin(message):
        return
    status_msg = await message.reply("🧠 Обновляю заметки...")
    updated = await update_long_memory()
    await status_msg.edit_text(
        "🧠 Заметки обновлены. Посмотреть: /memory"
        if updated
        else "🧠 Не обновил: за сутки мало переписки или Gemini не ответил. Старые заметки сохранены."
    )


@router.message(Command("mood", "настроение"))
async def mood_cmd(message: types.Message) -> None:
    """Какое у Мудреца сегодня настроение (образ меняется каждый день)."""
    if message.chat.id != CHAT_ID:
        return
    name, description = today_mood()
    await message.reply(
        f"🎭 <b>Настроение Мудреца сегодня:</b> {html.escape(name)}\n<i>{html.escape(description)}</i>"
    )


@router.message(Command("who", "ктосказал", "кто"))
async def who_said_cmd(message: types.Message) -> None:
    """Игра «Кто это сказал?»: случайная фраза из чата за месяц и кнопки с вариантами."""
    if message.chat.id != CHAT_ID:
        return
    quote, counts = await asyncio.gather(get_random_quote(days=30), get_user_message_counts(days=30))
    names = [name for name, _ in counts]
    if not quote or len(names) < 2:
        await message.reply("🕵️ Пока мало переписки для игры. Поболтайте ещё, потом сыграем.")
        return

    author, text, said_at = quote
    if text.startswith("[Голосовое сообщение]: "):
        text = text.split(": ", 1)[1]
    options = whosaid.build_options(author, names)
    game_id = whosaid.new_game(author, options, said_at)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=name, callback_data=f"who:{game_id}:{i}")]
            for i, name in enumerate(options)
        ]
    )
    await message.answer(
        f"🕵️ <b>Кто это сказал?</b>\n\n<i>«{html.escape(text)}»</i>", reply_markup=keyboard
    )


@router.callback_query(F.data.startswith("who:"))
async def who_said_answer(callback: types.CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    game = whosaid.get_game(parts[1]) if len(parts) == 3 else None
    if game is None or game.solved:
        await callback.answer("Эта игра уже закончилась. Новая: /who")
        return
    try:
        choice = game.options[int(parts[2])]
    except (ValueError, IndexError):
        await callback.answer()
        return

    player = callback.from_user.first_name or "Кто-то"
    if choice != game.answer:
        await callback.answer(f"❌ Мимо, это не {choice}")
        return

    game.solved = True
    score = await whosaid.add_point(player)
    when = game.said_at.astimezone(TZ).strftime("%d.%m.%Y")
    await callback.answer("✅ В точку!")
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            f"{callback.message.html_text}\n\n✅ <b>{html.escape(player)}</b> угадал(а): это "
            f"<b>{html.escape(game.answer)}</b>, {when}. Очков у {html.escape(player)}: {score}",
            reply_markup=None,
        )


@router.message(Command("score", "очки"))
async def who_said_scores(message: types.Message) -> None:
    if message.chat.id != CHAT_ID:
        return
    top = await whosaid.leaderboard()
    if not top:
        await message.reply("🕵️ В «Кто это сказал?» ещё никто не угадывал. Начать: /who")
        return
    lines = [f"{i}. {html.escape(name)} — {points}" for i, (name, points) in enumerate(top, 1)]
    await message.reply("🏆 <b>Лучшие сыщики:</b>\n" + "\n".join(lines))


@router.message(Command("test"))
async def test_cmd(message: types.Message, bot: Bot) -> None:
    # Без заданного ADMIN_ID команда закрыта для всех (раньше была открыта любому)
    if not ADMIN_ID or not message.from_user or str(message.from_user.id) != str(ADMIN_ID):
        await message.reply("🧙 Только хранитель знаний (администратор) может вызывать это испытание.")
        return

    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    await message.answer("🧠 Создаю тестовую мысль...")
    await create_daily_post(bot, force=True)


@router.message(Command("summary"))
async def summary_cmd(message: types.Message, bot: Bot) -> None:
    """Краткая выжимка того, что произошло в чате за 12 часов."""
    # История принадлежит только основному чату: в личке и в чужих чатах не отвечаем
    if message.chat.id != CHAT_ID:
        return
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    status_msg = await message.reply("🔮 Вспоминаю, о чём вы шептались...")

    summary_text = await generate_summary()
    safe_summary = html.escape(summary_text)
    await status_msg.edit_text(f"📜 <b>Краткая выжимка разговоров:</b>\n\n{safe_summary}")


@router.message(Command("probe", "пробей", "факт", "пруф", "спор", "чек"))
async def probe_cmd(message: types.Message, bot: Bot) -> None:
    """Фактчекер споров по фактам с выходом в поиск."""
    # Только основной чат: иначе посторонние тратят лимит Gemini
    if message.chat.id != CHAT_ID:
        return
    parts = (message.text or "").split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    if not query and message.reply_to_message:
        reply_text = message.reply_to_message.text or message.reply_to_message.caption
        if reply_text:
            query = reply_text.strip()

    if not query:
        await message.reply(
            "Напиши тему спора или вопрос, например: <code>/пробей сколько весит баррель нефти</code>"
        )
        return

    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    verdict = await fact_check_dispute(query)
    await message.reply(f"⚖️ <b>Вердикт Мудреца:</b>\n\n{html.escape(verdict)}")


# =====================================================
# MEDIA, VOICE & STICKERS
# =====================================================

@router.message(F.voice)
async def handle_voice_message(message: types.Message, bot: Bot) -> None:
    """Расшифровка голосовых; дальше голосовое обрабатывается как обычный текст."""
    if message.chat.id != CHAT_ID or not message.voice:
        return

    username = _sender_name(message)
    try:
        file = await bot.get_file(message.voice.file_id)
        voice_buffer = io.BytesIO()
        await bot.download_file(file.file_path, destination=voice_buffer)

        transcribed = await transcribe_voice_bytes(voice_buffer.getvalue())
        if not transcribed:
            return

        await save_message(username, f"[Голосовое сообщение]: {transcribed}")
        await message.reply(
            f"🗣 <b>{html.escape(username)}</b> (расшифровка):\n<i>«{html.escape(transcribed)}»</i>"
        )
        await respond_to_text(message, bot, transcribed)
    except Exception as e:
        logging.error(f"Ошибка обработки войса: {e}")


@router.message(F.photo | F.video_note)
async def handle_media_vision(message: types.Message, bot: Bot) -> None:
    """Фото и кружки: всегда попадают в историю; если бота позвали — он смотрит и отвечает."""
    if message.chat.id != CHAT_ID:
        return
    if message.from_user and message.from_user.is_bot:
        return

    username = _sender_name(message)
    caption = message.caption or ""
    kind = "фото" if message.photo else "видео-кружок"
    # Раньше неподписанные фото не сохранялись, и для памяти бота их как будто не было
    await save_message(username, f"[{kind.capitalize()}]" + (f": {caption}" if caption else ""))

    bot_info = await get_bot_info(bot)
    if not _is_addressed_to_bot(message, bot_info, caption.lower(), VISION_TRIGGER_KEYWORDS):
        return

    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    try:
        media_buffer = io.BytesIO()
        if message.photo:
            file = await bot.get_file(message.photo[-1].file_id)
            mime = "image/jpeg"
        else:
            file = await bot.get_file(message.video_note.file_id)
            mime = "video/mp4"
        await bot.download_file(file.file_path, destination=media_buffer)

        part = genai_types.Part.from_bytes(data=media_buffer.getvalue(), mime_type=mime)
        reply_quote = _format_reply_quote(message.reply_to_message, bot_info)
        reply = await ask_wisdom(
            caption, message.from_user, reply_quote, media=part, media_kind=kind
        )
        if not reply.is_empty:
            await send_bot_reply(message, bot, reply)
    except Exception as e:
        logging.error(f"Ошибка Vision: {e}")


@router.message(F.sticker)
async def handle_sticker(message: types.Message) -> None:
    """Запоминает стикеры друзей, чтобы бот потом мог отвечать ими сам."""
    if message.chat.id != CHAT_ID or not message.sticker:
        return
    if message.from_user and message.from_user.is_bot:
        return
    sticker = message.sticker
    try:
        await remember_sticker(sticker.file_id, sticker.file_unique_id, sticker.emoji)
        await save_message(_sender_name(message), f"[Стикер {sticker.emoji or ''}]".replace(" ]", "]"))
    except Exception as e:
        logging.warning(f"Не удалось сохранить стикер: {e}")


# =====================================================
# CHAT COLLECTOR & INTERACTIVE DIALOGUE
# =====================================================

@router.message(F.text)
async def collect(message: types.Message, bot: Bot) -> None:
    if message.chat.id != CHAT_ID or not message.text or message.text.startswith("/"):
        return
    if message.from_user and message.from_user.is_bot:
        return

    username = _sender_name(message)
    try:
        await save_message(username, message.text)
        # Текст личной переписки в логи не пишем, только факт сообщения
        logging.info(f"Сообщение от {username} ({len(message.text)} симв.)")

        await respond_to_text(message, bot, message.text)
    except Exception as e:
        logging.error(f"Ошибка обработки сообщения: {e}")
