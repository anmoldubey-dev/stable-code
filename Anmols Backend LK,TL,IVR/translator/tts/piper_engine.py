# ==========================================================
# APPLICATION FLOW OVERVIEW
# ==========================================================
# 1. PiperTTSEngine        -> Non-blocking async Piper TTS engine
# 2. __init__()            -> Register ONNX voice models by language code
# 3. synthesize()          -> Async: lookup model -> run_in_executor(_run_piper)
# 4. _run_piper()          -> Blocking: subprocess piper.exe -> WAV bytes
# 5. supported_languages() -> Return list of registered language codes
#
# PIPELINE FLOW
# text + lang
#    ||
# PiperTTSEngine.synthesize  ->  self._voices.get(lang)  ->  model_path
#    ||
# loop.run_in_executor(None, _run_piper, text, model_path)
#    ||
# _run_piper  ->  tempfile  ->  subprocess.run(piper.exe)
#    ||
# WAV bytes returned  ->  base64 encoded  ->  sent via WebSocket
# ==========================================================

"""
translator/tts/piper_engine.py
──────────────────────────────
Piper TTS engine — runs Piper in a thread-pool executor so the asyncio
event loop is never blocked.

On Windows, writing WAV binary to stdout via a pipe corrupts the data
because the C runtime opens pipes in text mode, converting 0x0A → 0x0D 0x0A.
We avoid this by writing to a temp file (always binary) and reading it back.

Voice map example
-----------------
{
    "en": "/abs/path/to/en_US-lessac-medium.onnx",
    "hi": "/abs/path/to/hi_IN-priyamvada-medium.onnx",
}
"""

import asyncio
import logging
import os
import subprocess
import tempfile
from typing import Dict

logger = logging.getLogger(__name__)


# --------------------------------------------------
# Non-blocking async Piper TTS engine using thread-pool executor
# --------------------------------------------------
class PiperTTSEngine:
    """
    Non-blocking TTS engine wrapping the Piper binary.

    Designed to be instantiated **once** at startup and reused
    concurrently across WebSocket sessions.
    """

    # --------------------------------------------------
    # Register ONNX voice models by language code, validate paths
    # Flow:
    #   piper_exe + voice_map
    #     ||
    #   isfile checks
    #     ||
    #   self._voices dict populated
    # --------------------------------------------------
    def __init__(self, piper_exe: str, voice_map: Dict[str, str]):
        if not os.path.isfile(piper_exe):
            raise FileNotFoundError(
                f"Piper executable not found: {piper_exe}"
            )
        self._piper_exe = piper_exe
        self._voices: Dict[str, str] = {}

        for lang, model_path in voice_map.items():
            if os.path.isfile(model_path):
                self._voices[lang] = model_path
                logger.info(
                    "Piper voice registered: %s → %s",
                    lang,
                    os.path.basename(model_path),
                )
            else:
                logger.warning(
                    "Piper voice model not found for '%s': %s", lang, model_path
                )

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    # --------------------------------------------------
    # Async TTS — offload blocking Piper call to thread pool
    # Flow:
    #   text + lang
    #     ||
    #   Lookup ONNX model
    #     ||
    #   run_in_executor(_run_piper)
    #     ||
    #   WAV bytes
    # --------------------------------------------------
    async def synthesize(self, text: str, lang: str) -> bytes:
        """
        Convert *text* to WAV audio bytes using Piper.

        Runs Piper in a thread-pool executor so the event loop stays free.
        Audio is written to a temp file (binary-safe on Windows) then read back.

        Returns
        -------
        bytes
            Raw WAV file bytes, or ``b""`` if *text* is empty.
        """
        text = (text or "").strip()
        if not text:
            return b""

        model_path = self._voices.get(lang)
        if not model_path:
            raise ValueError(
                f"No Piper voice available for language '{lang}'. "
                f"Registered: {list(self._voices.keys())}"
            )

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._run_piper, text, model_path
        )

    # ------------------------------------------------------------------ #
    #  Internal — runs in a thread                                         #
    # ------------------------------------------------------------------ #

    # --------------------------------------------------
    # Blocking Piper subprocess call via temp file (Windows-safe)
    # Flow:
    #   text + model_path
    #     ||
    #   tempfile.NamedTemporaryFile
    #     ||
    #   subprocess.run(piper.exe)
    #     ||
    #   Read + return WAV bytes
    # --------------------------------------------------
    def _run_piper(self, text: str, model_path: str) -> bytes:
        """Blocking Piper call — always invoked via run_in_executor."""
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        try:
            result = subprocess.run(
                [
                    self._piper_exe,
                    "--model",            model_path,
                    "--output_file",      tmp.name,  # file = always binary, no pipe corruption
                    "--noise_scale",      "0.667",   # Piper default — natural voice
                    "--noise_w",          "0.8",     # Piper default — natural rhythm
                    "--length_scale",     "1.0",     # normal speech rate
                    "--sentence_silence", "0.1",     # small gap between sentences
                    "-q",                            # suppress Piper log noise
                ],
                input=text.encode("utf-8"),
                capture_output=True,
            )

            if result.returncode != 0:
                err = result.stderr.decode("utf-8", errors="replace").strip()
                logger.error(
                    "[TTS] Piper failed (rc=%d): %s | text=%r",
                    result.returncode, err, text[:80],
                )
                raise RuntimeError(
                    f"Piper failed (rc={result.returncode}): {err}"
                )

            if not os.path.isfile(tmp.name) or os.path.getsize(tmp.name) == 0:
                logger.warning("[TTS] Piper produced no output for text: %r", text[:80])
                return b""

            with open(tmp.name, "rb") as fh:
                wav = fh.read()

            logger.info("[TTS] Piper produced %d bytes WAV for %r", len(wav), text[:40])
            return wav

        finally:
            if os.path.exists(tmp.name):
                os.unlink(tmp.name)

    # --------------------------------------------------
    # Return list of registered language codes
    # Flow:
    #   self._voices.keys()
    #     ||
    #   list[str]
    # --------------------------------------------------
    def supported_languages(self) -> list[str]:
        return list(self._voices.keys())
