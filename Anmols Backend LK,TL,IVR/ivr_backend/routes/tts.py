# ==========================================================
# APPLICATION FLOW OVERVIEW
# ==========================================================
# 1. generate_tts() -> POST /tts/generate — text to WAV via Piper
#
# PIPELINE FLOW
# POST /tts/generate  {text, lang, voice_model}
#    ||
# tts_service.synthesize(text, lang, voice_model)
#    ||
# Piper subprocess  ->  WAV bytes
#    ||
# StreamingResponse (audio/wav)
# ==========================================================
"""
ivr_backend/routes/tts.py
POST /tts/generate — Piper TTS proxy, no authentication required.
GET  /tts/voices   — Return voice registry (auto-scanned ONNX models).
"""
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional

from ..services.tts_service import generate_speech

router = APIRouter(tags=["tts"])

# ── Voice registry (mirrors backend/app.py lifespan logic) ────────────────────
_MODELS_DIR = (
    Path(__file__).parent.parent.parent / "backend" / "tts" / "piper" / "models"
)
# Marathi shares Hindi (no native mr model)
_LANG_FALLBACK = {"mr": "hi"}


def _build_registry() -> dict:
    registry: dict = {}
    if _MODELS_DIR.exists():
        for onnx in sorted(_MODELS_DIR.rglob("*.onnx")):
            stem = onnx.stem
            lang_code = stem.split("_")[0].lower()
            registry.setdefault(lang_code, [])
            if not any(v["name"] == stem for v in registry[lang_code]):
                registry[lang_code].append(
                    {"name": stem, "model_path": str(onnx)}
                )
    for lang_code, src_lang in _LANG_FALLBACK.items():
        if lang_code not in registry and src_lang in registry:
            registry[lang_code] = list(registry[src_lang])
    return registry


@router.get("/voices")
def tts_voices():
    """Return auto-scanned voice registry: {lang_code: [{name, model_path}]}."""
    return _build_registry()


class TTSRequest(BaseModel):
    text: str
    language: Optional[str] = "English"
    model_path: Optional[str] = None   # if set, use this ONNX directly


@router.post("/generate")
async def tts_generate(req: TTSRequest):
    if not req.text.strip():
        raise HTTPException(status_code=422, detail="text is required")
    try:
        wav = await generate_speech(req.text, req.language or "English", req.model_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"TTS error: {exc}")
    return Response(content=wav, media_type="audio/wav")
