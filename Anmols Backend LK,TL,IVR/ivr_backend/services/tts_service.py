# ==========================================================
# APPLICATION FLOW OVERVIEW
# ==========================================================
# 1. synthesize() -> Text + lang + voice_model -> WAV bytes via Piper
#
# PIPELINE FLOW
# synthesize(text, lang, voice_model)
#    ||
# voice_mapper.resolve(lang, voice_model)  ->  .onnx path
#    ||
# subprocess.run(piper.exe --model <onnx> --output_file <tmp.wav>)
#    ||
# Read tmp WAV bytes  ->  return bytes to route
# ==========================================================
"""
ivr_backend/services/tts_service.py
Calls Piper TTS executable directly (same approach as backend/app.py).
Runs in an asyncio thread-pool executor so it never blocks the event loop.
"""
import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .voice_mapper import get_model_key

# ── Locate Piper relative to project root ─────────────────────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent.parent
_PIPER_EXE    = str(_PROJECT_ROOT / "backend" / "tts" / "piper" / "piper.exe")
_MODELS_DIR   = _PROJECT_ROOT / "backend" / "tts" / "piper" / "models"

# ── Model key → ONNX file path ────────────────────────────────────────────────
# Primary locations first (models/en/, models/hi/ for old installs),
# then fallback to models/common/ where all new voices live.
_VOICE_MODELS: dict[str, str] = {
    "en": str(_MODELS_DIR / "en"     / "en_US-lessac-medium.onnx"),
    "hi": str(_MODELS_DIR / "hi"     / "hi_IN-priyamvada-medium.onnx"),
    "es": str(_MODELS_DIR / "common" / "es_MX-claude-high.onnx"),
    "fr": str(_MODELS_DIR / "common" / "fr_FR-siwis-medium.onnx"),
    "ne": str(_MODELS_DIR / "common" / "ne_NP-chitwan-medium.onnx"),
    "te": str(_MODELS_DIR / "common" / "te_IN-padmavathi-medium.onnx"),
    "ml": str(_MODELS_DIR / "common" / "ml_IN-meera-medium.onnx"),
    "ru": str(_MODELS_DIR / "common" / "ru_RU-irina-medium.onnx"),
    "ar": str(_MODELS_DIR / "common" / "ar_JO-kareem-medium.onnx"),
    "zh": str(_MODELS_DIR / "common" / "zh_CN-huayan-medium.onnx"),
}


def _resolve_model(model_key: str) -> str:
    """Return model path from key, falling back to common/ then English if missing."""
    path = _VOICE_MODELS.get(model_key, _VOICE_MODELS["en"])
    if not os.path.exists(path):
        # Try common/ subfolder variants
        common = _MODELS_DIR / "common"
        if common.exists():
            for f in common.glob(f"{model_key}_*.onnx"):
                return str(f)
        # Last resort: English
        return _VOICE_MODELS["en"]
    return path


def _piper_sync(text: str, model_path: str) -> bytes:
    """Blocking Piper call with full model path — run via run_in_executor."""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    try:
        res = subprocess.run(
            [
                _PIPER_EXE,
                "--model",            model_path,
                "--output_file",      tmp.name,
                "--noise_scale",      "0.667",
                "--noise_w",          "0.8",
                "--length_scale",     "1.0",
                "--sentence_silence", "0.1",
                "-q",
            ],
            input=text.encode("utf-8"),
            capture_output=True,
        )
        if res.returncode != 0:
            raise RuntimeError(res.stderr.decode("utf-8", errors="replace").strip())
        with open(tmp.name, "rb") as fh:
            return fh.read()
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)


async def generate_speech(
    text: str,
    language: str = "English",
    model_path: Optional[str] = None,
) -> bytes:
    """
    Async wrapper — offloads blocking Piper call to thread pool.

    model_path: if provided, used directly (bypasses language lookup).
                This lets the frontend pick a specific speaker model.
    """
    if model_path and os.path.exists(model_path):
        resolved = model_path
    else:
        model_key = get_model_key(language)
        resolved  = _resolve_model(model_key)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _piper_sync, text.strip(), resolved)
