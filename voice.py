from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

from faster_whisper import WhisperModel
from telegram import Voice

from config import config


class VoiceTranscriptionError(Exception):
    pass


_model: WhisperModel | None = None


def _get_model() -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel(config.whisper_model_size, device="cpu", compute_type="int8")
    return _model


def _convert_to_wav(source: Path, target: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(source), "-ar", "16000", "-ac", "1", str(target)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _transcribe_wav(path: Path) -> str:
    model = _get_model()
    segments, _info = model.transcribe(
        str(path),
        beam_size=5,
        language=config.whisper_language,
        vad_filter=True,
    )
    text = " ".join(segment.text.strip() for segment in segments if segment.text.strip())
    if not text:
        raise VoiceTranscriptionError("I could not hear any speech in that voice note.")
    return text


async def transcribe_voice_note(voice: Voice) -> str:
    ogg_path: Path | None = None
    wav_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as ogg_file:
            ogg_path = Path(ogg_file.name)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav_file:
            wav_path = Path(wav_file.name)

        telegram_file = await voice.get_file()
        await telegram_file.download_to_drive(custom_path=str(ogg_path))
        await asyncio.to_thread(_convert_to_wav, ogg_path, wav_path)
        return await asyncio.to_thread(_transcribe_wav, wav_path)
    except FileNotFoundError as exc:
        raise VoiceTranscriptionError("ffmpeg is not installed or is not available on PATH.") from exc
    except VoiceTranscriptionError:
        raise
    except subprocess.CalledProcessError as exc:
        raise VoiceTranscriptionError("I could not convert that voice note for transcription.") from exc
    except Exception as exc:
        raise VoiceTranscriptionError("I could not transcribe that voice note. Please try again or send text.") from exc
    finally:
        for path in (ogg_path, wav_path):
            if path:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
