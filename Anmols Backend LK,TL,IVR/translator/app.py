# ==========================================================
# APPLICATION FLOW OVERVIEW
# ==========================================================
# 1. lifespan()     -> Load STT, NMT, TTS models at startup
# 2. index()        -> Serve translator frontend via Jinja2 (GET /)
# 3. health()       -> Return service health and loaded models (GET /health)
# 4. ws_translate() -> WebSocket translation session (WS /ws/translate)
#
# PIPELINE FLOW
# lifespan  ->  StreamingTranscriber + TranslatorEngine + PiperTTSEngine
#    ||
# ws_translate  ->  websocket.accept()
#    ||
# StreamController(ws, models)  ->  await controller.run()
#    ||
# Client PCM audio  ->  STT  ->  NMT  ->  TTS  ->  base64 WAV response
# ==========================================================

"""
translator/app.py
─────────────────
FastAPI entrypoint for the Real-Time Bidirectional Speech Translator.

Run (from the translator/ directory):
    python -m uvicorn app:app --host 0.0.0.0 --port 9000 --reload

Run (from the project root):
    python -m uvicorn translator.app:app --host 0.0.0.0 --port 9000
"""

import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ── Path bootstrap ────────────────────────────────────────────────────────────
# Ensure both the translator package root AND the project root are importable
# regardless of which directory uvicorn is launched from.
TRANSLATOR_ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TRANSLATOR_ROOT)

for _p in (TRANSLATOR_ROOT, PROJECT_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Piper configuration ───────────────────────────────────────────────────────
_PIPER_DIR = os.path.join(PROJECT_ROOT, "backend", "tts", "piper")
PIPER_EXE = os.path.join(_PIPER_DIR, "piper.exe")
_MODELS_DIR = os.path.join(_PIPER_DIR, "models")

VOICE_MAP: dict[str, str] = {
    "en": os.path.join(_MODELS_DIR, "en", "en_US-lessac-medium.onnx"),
    "hi": os.path.join(_MODELS_DIR, "hi", "hi_IN-priyamvada-medium.onnx"),
    # Extend here for future languages:
    # "ja": os.path.join(_MODELS_DIR, "ja", "ja_JP-<voice>.onnx"),
}

# ── Global model registry ─────────────────────────────────────────────────────
# Populated in lifespan; passed to StreamController at connection time.
models: dict = {}


# ── Lifespan ──────────────────────────────────────────────────────────────────
# --------------------------------------------------
# Load STT, NMT, TTS models at startup; release on shutdown
# Flow:
#   startup
#     ||
#   StreamingTranscriber -> TranslatorEngine -> PiperTTSEngine
#     ||
#   models dict ready -> yield -> models.clear()
# --------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load all heavy models once at startup; release on shutdown."""

    logger.info("═" * 60)
    logger.info("  Translator Service — model loading …")
    logger.info("═" * 60)

    # 1. STT
    logger.info("[1/3] Loading Whisper STT model …")
    from stt.transcriber import StreamingTranscriber
    models["stt"] = StreamingTranscriber(model_size="small")
    logger.info("[1/3] STT ready.")

    # 2. Translation
    logger.info("[2/3] Loading M2M-100 translation model …")
    from translation.translator_engine import TranslatorEngine
    models["translator"] = TranslatorEngine()
    logger.info("[2/3] Translation ready.")

    # 3. TTS
    logger.info("[3/3] Initialising Piper TTS engine …")
    from tts.piper_engine import PiperTTSEngine
    models["tts"] = PiperTTSEngine(piper_exe=PIPER_EXE, voice_map=VOICE_MAP)
    logger.info("[3/3] TTS ready.")

    logger.info("═" * 60)
    logger.info("  All models loaded.  Listening on http://localhost:9000")
    logger.info("═" * 60)

    yield  # ← app is live here

    models.clear()
    logger.info("Models released.  Shutdown complete.")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Real-Time Bidirectional Speech Translator",
    version="1.0.0",
    lifespan=lifespan,
)

# Static files and templates resolved relative to this file, so they work
# regardless of the launch directory.
app.mount(
    "/static",
    StaticFiles(directory=os.path.join(TRANSLATOR_ROOT, "static")),
    name="static",
)
templates = Jinja2Templates(
    directory=os.path.join(TRANSLATOR_ROOT, "templates")
)


# ── Routes ────────────────────────────────────────────────────────────────────
# --------------------------------------------------
# Serve translator single-page frontend via Jinja2 template
# Flow:
#   GET /
#     ||
#   templates.TemplateResponse('index.html')
#     ||
#   HTMLResponse
# --------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


# --------------------------------------------------
# Return service health status and list of loaded model keys
# Flow:
#   GET /health
#     ||
#   list(models.keys())
#     ||
#   {status, models_loaded}
# --------------------------------------------------
@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "models_loaded": list(models.keys()),
    }


# --------------------------------------------------
# Accept WebSocket, create StreamController, run session pipeline
# Flow:
#   Client Connect
#     ||
#   websocket.accept()
#     ||
#   StreamController(ws, models)
#     ||
#   await controller.run()
# --------------------------------------------------
@app.websocket("/ws/translate")
async def ws_translate(websocket: WebSocket) -> None:
    """
    WebSocket endpoint for the real-time translation pipeline.

    Protocol
    ────────
    Client → Server
      • JSON  { "action": "start", "source_lang": "hi", "target_lang": "en" }
      • JSON  { "action": "stop" }
      • JSON  { "action": "flush" }   ← force-process current buffer
      • Binary: raw Float32 PCM mono @ 16 kHz  (ArrayBuffer from AudioWorklet)

    Server → Client (JSON)
      • { "type": "status",      "message": "listening" | "processing" }
      • { "type": "transcript",  "text": "..." }
      • { "type": "translation", "text": "..." }
      • { "type": "audio",       "data": "<base64-WAV>" }
      • { "type": "error",       "message": "..." }
    """
    await websocket.accept()
    logger.info(
        "WebSocket connection from %s", websocket.client
    )
    from streaming.stream_controller import StreamController
    controller = StreamController(websocket=websocket, models=models)
    await controller.run()
