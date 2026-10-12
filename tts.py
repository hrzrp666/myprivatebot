import logging
import random
import re
import edge_tts

from config import VOICE_ACTOR, VOICE_PRESETS

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_CLEAN_TEXT_RE = re.compile(r"[^\w\s.,!?-]")


async def text_to_speech_bytes(text: str) -> bytes | None:
    """Конвертирует текст ответа со случайным угарным стилем озвучки."""
    try:
        clean_text = _HTML_TAG_RE.sub("", text)
        clean_text = _CLEAN_TEXT_RE.sub("", clean_text).strip()
        if not clean_text:
            return None

        preset = random.choice(VOICE_PRESETS)
        communicate = edge_tts.Communicate(
            clean_text,
            VOICE_ACTOR,
            pitch=preset["pitch"],
            rate=preset["rate"],
        )

        audio_bytes = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_bytes.extend(chunk["data"])
        return bytes(audio_bytes) if audio_bytes else None
    except Exception as e:
        logging.warning(f"Ошибка генерации голоса Edge-TTS: {e}")
        return None
