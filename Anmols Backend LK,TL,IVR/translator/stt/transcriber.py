# ==========================================================
# APPLICATION FLOW OVERVIEW
# ==========================================================
# 1. StreamingTranscriber  -> Thread-safe Whisper for Float32 PCM chunks
# 2. __init__()            -> Detect CUDA/CPU -> load WhisperModel (small)
# 3. transcribe_pcm()      -> RMS normalize -> temp WAV -> Whisper -> text
# 4. _write_wav()          -> Float32 PCM -> 16kHz mono WAV file
# 5. _best_device()        -> Detect CUDA or fallback to CPU
#
# PIPELINE FLOW
# Float32 PCM @ 16kHz (from browser AudioWorklet)
#    ||
# transcribe_pcm  ->  RMS normalize (target 0.12, max 30x gain)
#    ||
# _write_wav  ->  temp WAV file
#    ||
# WhisperModel.transcribe (beam=5, no_speech_threshold=0.45)
#    ||
# Transcript text string  ->  returned to StreamController
# ==========================================================

"""
translator/stt/transcriber.py
─────────────────────────────
Faster-Whisper based streaming transcriber.
Processes Float32 PCM audio at 16 kHz (mono) captured from the browser.

Optimised settings for low-latency chunk transcription:
  - model  : small  (fast + accurate enough for real-time)
  - device : CUDA if available, else CPU
  - compute: int8 (CPU) / float16 (CUDA)
  - beam   : 2  (better accuracy than 1, faster than 3)
  - temp   : 0  (deterministic, suppresses hallucination variance)
  - VAD    : enabled (skip silent chunks automatically)
"""

import logging
import os
import tempfile
import time
import wave

import numpy as np
from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

# Languages supported by Whisper that we accept as hints.
# Must stay in sync with LANGUAGE_CONFIG keys in backend/core/config.py.
# Missing a language here causes silent auto-detect fallback which can
# misidentify closely related languages (e.g. Marathi detected as Hindi).
_WHISPER_LANGS = {
    "en", "hi", "mr", "ml", "te", "ta",   # English + Indian languages
    "ar", "es", "fr", "ru", "ne", "zh",   # Arabic, European, Nepali, Chinese
    "ja", "de", "pt", "ko", "it", "nl", "tr",  # extras Whisper handles well
}


# --------------------------------------------------
# Real-time Whisper transcriber for Float32 PCM chunks from browser
# --------------------------------------------------
class StreamingTranscriber:
    """
    Thread-safe Whisper transcriber for short PCM chunks.

    Usage
    -----
    Instantiate once at startup, then call ``transcribe_pcm`` from a
    thread-pool executor (``asyncio.run_in_executor``) to keep the event
    loop unblocked.
    """

    # --------------------------------------------------
    # Detect CUDA/CPU and load faster-whisper small model
    # Flow:
    #   model_size
    #     ||
    #   _best_device()
    #     ||
    #   WhisperModel
    #     ||
    #   self.model ready
    # --------------------------------------------------
    def __init__(self, model_size: str = "small"):
        device, compute_type = self._best_device()
        logger.info(
            "Loading Whisper '%s' on %s (%s) …", model_size, device, compute_type
        )
        self.model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
            cpu_threads=6,
        )
        logger.info("Whisper ready.")

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    # --------------------------------------------------
    # Transcribe Float32 PCM chunk to text with loudness normalization
    # Flow:
    #   Float32 PCM (16kHz)
    #     ||
    #   Normalize RMS
    #     ||
    #   Write temp WAV
    #     ||
    #   Whisper STT (beam=2)
    #     ||
    #   Text Output
    # --------------------------------------------------
    def transcribe_pcm(
        self,
        pcm: np.ndarray,
        language: str | None = None,
        initial_prompt: str | None = None,
    ) -> str:
        """
        Transcribe a Float32 mono PCM array sampled at **16 000 Hz**.

        Parameters
        ----------
        pcm : np.ndarray
            Float32 array with values in [-1, 1].
        language : str | None
            ISO-639-1 hint (e.g. ``'hi'``, ``'en'``).  Pass ``None`` for
            auto-detect.
        initial_prompt : str | None
            Optional text to prime Whisper's decoder. For Indian languages
            this should be realistic code-switching sentences so English
            technical words are kept as English rather than phonetically
            converted into Devanagari/other scripts.

        Returns
        -------
        str
            Transcribed text, or empty string if nothing was detected.
        """
        if pcm is None or len(pcm) == 0:
            return ""

        pcm = np.clip(pcm, -1.0, 1.0)

        # ── Loudness normalization ────────────────────────────────────────
        # Whisper was trained on audio with RMS ~0.1.  Laptop mics often
        # capture at RMS 0.002–0.01, causing Whisper to return nothing
        # even when the model can clearly detect the language.
        # Boost quiet audio to a target RMS of 0.1 (capped at 20x gain).
        current_rms = float(np.sqrt(np.mean(pcm ** 2)))
        if current_rms > 0.0005:          # skip if truly silent
            # Target RMS 0.12 (slightly above Whisper's 0.1 training point) —
            # gives it more signal headroom, especially for accented/soft speech.
            # Cap at 30x (was 20x) to handle WebRTC mics captured very quietly.
            gain = min(0.12 / current_rms, 30.0)
            if gain > 1.0:
                pcm = np.clip(pcm * gain, -1.0, 1.0)
                logger.debug("[STT] normalized  rms=%.5f → %.5f (gain=%.1fx)",
                             current_rms, min(current_rms * gain, 1.0), gain)

        lang_hint = language if language in _WHISPER_LANGS else None
        duration_sec = len(pcm) / 16_000

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        t0 = time.perf_counter()
        try:
            self._write_wav(tmp.name, pcm)
            segments, info = self.model.transcribe(
                tmp.name,
                language=lang_hint,
                # Prime decoder with realistic code-switching vocab so English words
                # embedded in Indian-language speech stay as English (e.g. "website"
                # instead of "वैप्साइती", "open" instead of "होपन").
                initial_prompt=initial_prompt,
                # beam=5: significantly more accurate than beam=2 for short utterances
                # (call-center turns are 2-10 words — beam=2 misses close alternatives).
                beam_size=5,
                best_of=1,
                temperature=0,
                condition_on_previous_text=False,
                # VAD disabled — our upstream RMS check already filters silence.
                # Whisper's built-in VAD is too aggressive on short (<2s) clips
                # and strips out all audio even when RMS is clearly non-zero.
                vad_filter=False,
                # Lowered from 0.6: keeps audio where Whisper is mildly uncertain.
                # 0.6 was silently dropping soft/accented speech at 0.45-0.55 confidence.
                no_speech_threshold=0.45,
                # Penalise repeated tokens to prevent Whisper looping the same
                # phrase (common hallucination on noisy/short clips with temperature=0).
                repetition_penalty=1.3,
            )
            result = " ".join(s.text.strip() for s in segments).strip()
            elapsed = (time.perf_counter() - t0) * 1000
            logger.info(
                "[STT] audio=%.2fs → detected_lang=%s(%.0f%%) → %dms → %r",
                duration_sec,
                info.language,
                info.language_probability * 100,
                elapsed,
                result[:80] if result else "(empty)",
            )
            return result
        finally:
            if os.path.exists(tmp.name):
                os.unlink(tmp.name)

    # ------------------------------------------------------------------ #
    #  Internals                                                           #
    # ------------------------------------------------------------------ #

    # --------------------------------------------------
    # Write Float32 PCM array to a 16kHz mono WAV file
    # Flow:
    #   path + Float32 PCM
    #     ||
    #   (pcm * 32767).astype(int16)
    #     ||
    #   wave.open + writeframes
    # --------------------------------------------------
    @staticmethod
    def _write_wav(path: str, pcm: np.ndarray) -> None:
        pcm_int16 = (pcm * 32_767).astype(np.int16)
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16_000)
            wf.writeframes(pcm_int16.tobytes())

    # --------------------------------------------------
    # Detect CUDA or fall back to CPU for Whisper
    # Flow:
    #   torch.cuda.is_available()
    #     ||
    #   Return ('cuda','float16') or ('cpu','int8')
    # --------------------------------------------------
    @staticmethod
    def _best_device() -> tuple[str, str]:
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda", "float16"
        except ImportError:
            pass
        return "cpu", "int8"
