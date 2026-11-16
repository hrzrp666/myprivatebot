import asyncio
import html
import io
import logging
import random
import re
from datetime import datetime, timedelta
from aiogram import Bot, F, Router, types
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import BufferedInputFile

from ai_service import (
    analyze_visual_media,
    ask_wisdom,
    check_ilya_ponto_radar,
    fact_check_dispute,
    generate_summary,
    maybe_join_conversation,
    summarize_media_url,
    transcribe_voice_bytes,
)
from config import (
    ADMIN_ID,
    CHAT_ID,
    LINK_TRIGGER_KEYWORDS,
    TZ,
    VISION_TRIGGER_KEYWORDS,
    VOICE_TRIGGER_KEYWORDS,
)
from database import save_message
from scheduler import create_daily_post
from tts import text_to_speech_bytes

router = Router()

_bot_info: types.User | None = None
_bot_info_lock = asyncio.Lock()
_last_random_comment_time = datetime.min.replace(tzinfo=TZ)
_URL_RE = re.compile(r"https?://\S+")


async def get_bot_info(bot: Bot) -> types.User:
    """Кэширует информацию о боте (с защитой от гонки при старте), чтобы не вызывать bot.get_me() повторно."""
    global _bot_info
    if _bot_info is None:
        async with _bot_info_lock:
            if _bot_info is None:
                _bot_info = await bot.get_me()
    return _bot_info


_BOT_NAME_TRIGGERS = ("мяумяур", "мудрец")
_BOT_WORD_RE = re.compile(r"\bбот\b", re.IGNORECASE)


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
    if is_reply or any(t in text_lower for t in _BOT_NAME_TRIGGERS) or _BOT_WORD_RE.search(text_lower):
        return True

    bot_username = (bot_info.username or "").lower()
    if bot_username and f"@{bot_username}" in text_lower:
        return True

    if extra_keywords and any(w in text_lower for w in extra_keywords):
        return True

    return False


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
        "• Каждое воскресенье в 20:00 — Итоги недели\n"
        "• Кидай голосовые (я расшифрую) и фотки/кружочки (я заценю)!"
    )


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
# MEDIA & VOICE HANDLERS
# =====================================================

@router.message(F.voice)
async def handle_voice_message(message: types.Message, bot: Bot) -> None:
    """Моментальная расшифровка голосовых сообщений."""
    if message.chat.id != CHAT_ID or not message.voice:
        return

    username = (
        message.from_user.first_name
        if message.from_user and message.from_user.first_name
        else "Участник"
    )
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

        bot_info = await get_bot_info(bot)
        if _is_addressed_to_bot(message, bot_info, transcribed.lower()):
            await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
            answer = await ask_wisdom(transcribed, message.from_user)
            await save_message("Мудрец Мяумяура", answer)
            await message.answer(f"🧙 {html.escape(answer)}")
    except Exception as e:
        logging.error(f"Ошибка обработки войса: {e}")


@router.message(F.photo | F.video_note)
async def handle_media_vision(message: types.Message, bot: Bot) -> None:
    """Gemini Vision: реакция на фото и видео-кружочки."""
    if message.chat.id != CHAT_ID:
        return

    username = (
        message.from_user.first_name
        if message.from_user and message.from_user.first_name
        else "Участник"
    )
    caption = message.caption or ""
    bot_info = await get_bot_info(bot)

    if not _is_addressed_to_bot(message, bot_info, caption.lower(), VISION_TRIGGER_KEYWORDS):
        return

    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    try:
        media_buffer = io.BytesIO()
        if message.photo:
            file = await bot.get_file(message.photo[-1].file_id)
            await bot.download_file(file.file_path, destination=media_buffer)
            mime = "image/jpeg"
        elif message.video_note:
            file = await bot.get_file(message.video_note.file_id)
            await bot.download_file(file.file_path, destination=media_buffer)
            mime = "video/mp4"
        else:
            return

        comment = await analyze_visual_media(
            media_buffer.getvalue(), mime, caption, message.from_user
        )
        await message.reply(f"🧙 {html.escape(comment)}")
        await save_message(username, f"[Медиа]: {caption} (Бот: {comment})")
    except Exception as e:
        logging.error(f"Ошибка Vision: {e}")


# =====================================================
# CHAT COLLECTOR & INTERACTIVE DIALOGUE
# =====================================================

@router.message(F.text)
async def collect(message: types.Message, bot: Bot) -> None:
    global _last_random_comment_time

    if message.chat.id != CHAT_ID or not message.text or message.text.startswith("/"):
        return
    if message.from_user and message.from_user.is_bot:
        return

    username = (
        message.from_user.first_name
        if message.from_user and message.from_user.first_name
        else "Участник"
    )
    text_lower = message.text.lower()

    try:
        await save_message(username, message.text)
        # Текст личной переписки в логи не пишем, только факт сообщения
        logging.info(f"Сообщение от {username} ({len(message.text)} симв.)")

        # 1. Радар понтов для Ильи
        ponto_alert = await check_ilya_ponto_radar(message)
        if ponto_alert:
            await message.reply(ponto_alert)
            return

        # 2. Быстрый пересказ ссылок
        url_match = _URL_RE.search(message.text)
        if url_match and any(w in text_lower for w in LINK_TRIGGER_KEYWORDS):
            await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
            summary_link = await summarize_media_url(url_match.group(0), message.from_user)
            await save_message("Мудрец Мяумяура", summary_link)
            await message.reply(f"🎬 <b>Суть ссылки:</b>\n{html.escape(summary_link)}")
            return

        # 3. Прямое обращение (по имени «мяумяура», тегу или Reply)
        bot_info = await get_bot_info(bot)
        if _is_addressed_to_bot(message, bot_info, text_lower):
            await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
            reply_quote = (
                (message.reply_to_message.text or message.reply_to_message.caption)
                if message.reply_to_message
                else None
            )
            answer = await ask_wisdom(message.text, message.from_user, reply_quote)
            await save_message("Мудрец Мяумяура", answer)

            # Голосовой ответ по прямой просьбе
            if any(w in text_lower for w in VOICE_TRIGGER_KEYWORDS):
                await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.RECORD_VOICE)
                audio_bytes = await text_to_speech_bytes(answer)
                if audio_bytes:
                    voice_file = BufferedInputFile(audio_bytes, filename="wisdom.mp3")
                    await message.reply_voice(voice=voice_file, caption=f"🧙 {html.escape(answer)}")
                    return

            await message.reply(f"🧙 {html.escape(answer)}")
            return

        # 4. Спонтанное вклинивание в диалог (шанс 10%, не чаще раза в 10 минут)
        now = datetime.now(TZ)
        if (now - _last_random_comment_time) > timedelta(minutes=10) and random.random() < 0.10:
            remark = await maybe_join_conversation(message.from_user)
            if remark:
                _last_random_comment_time = now
                await save_message("Мудрец Мяумяура", remark)
                await message.reply(f"🧙 {html.escape(remark)}")

    except Exception as e:
        logging.error(f"Ошибка обработки сообщения: {e}")
