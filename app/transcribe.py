"""Optional voice-note transcription with faster-whisper.

WhatsApp voice notes arrive as OGG/Opus. We write the bytes to a temp file and
run faster-whisper locally (CPU works; a GPU is faster). If faster-whisper or
ffmpeg is not installed, we return None and the agent politely asks the user to
type instead.

Want a fuller local voice stack (mic in, TTS out)? See the sibling repo
voice-agent-starter: https://github.com/AleBrito124356/voice-agent-starter
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger("whatsapp_agent.transcribe")

_model = None
_model_size: Optional[str] = None


def _get_model(model_size: str):
    """Lazily load and cache a WhisperModel. Returns None if unavailable."""
    global _model, _model_size
    if _model is not None and _model_size == model_size:
        return _model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log.info("faster-whisper not installed; voice notes will not be transcribed.")
        return None
    try:
        _model = WhisperModel(model_size, device="auto", compute_type="int8")
        _model_size = model_size
        return _model
    except Exception as exc:  # pragma: no cover - model/download failure
        log.warning("Could not load whisper model '%s': %s", model_size, exc)
        return None


def transcribe_audio(audio_bytes: bytes, model_size: str = "base") -> Optional[str]:
    """Transcribe raw audio bytes. Returns the text, or None if unavailable."""
    model = _get_model(model_size)
    if model is None:
        return None

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = Path(tmp.name)
        segments, _info = model.transcribe(str(tmp_path), vad_filter=True)
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return text or None
    except Exception as exc:  # pragma: no cover - runtime decode failure
        log.warning("Transcription failed: %s", exc)
        return None
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
