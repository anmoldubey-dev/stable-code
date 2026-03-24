# ==========================================================
# APPLICATION FLOW OVERVIEW
# ==========================================================
# 1. StreamController         -> Full STT->NMT->TTS pipeline for one WebSocket session
# 2. __init__()               -> Init per-session state (ws, models, buffer, VAD flags)
# 3. run()                    -> WebSocket message loop + launch _process_loop task
# 4. _handle_control()        -> Parse JSON control: start/stop/flush/ping
# 5. _process_loop()          -> VAD state machine: detect speech/silence, flush
# 6. _process_buffer()        -> Core pipeline: STT -> guards -> dedup -> NMT -> TTS -> send
# 7. _send()                  -> Safe fire-and-forget WebSocket JSON send
# 8. _collapse_repetitions()  -> Collapse Whisper loop hallucinations (Guard C)
# 9. _extract_new_text()      -> Return only new words vs previous transcript (delta dedup)
#
# PIPELINE FLOW
# Client Audio (Float32 PCM binary WebSocket frames)
#    ||
# _process_loop  ->  VAD: RMS speech/silence state machine (every 0.5s)
#    ||
# _process_buffer  ->  audio snapshot  ->  STT (run_in_executor)
#    ||
# _collapse_repetitions  ->  Guard A / Guard B / Guard D
#    ||
# _extract_new_text (delta dedup)  ->  NMT (run_in_executor)
#    ||
# TTS.synthesize  ->  base64 WAV  ->  _send  ->  Browser
# ==========================================================

"""
translator/streaming/stream_controller.py
─────────────────────────────────────────
Real-time bidirectional translation pipeline for a single WebSocket session.

Pipeline
────────
  Browser mic (Float32 PCM @ 16 kHz)
      ↓  binary WebSocket frames
  Audio buffer  →  silence / duration gate
      ↓
  STT  (faster-whisper, thread-pool)
      ↓
  Delta deduplication  (skip already-translated text)
      ↓
  Translation  (MarianMT, thread-pool)
      ↓
  TTS  (Piper, async subprocess)
      ↓
  JSON over WebSocket  { type: "transcript" | "translation" | "audio" | "status" }

Concurrency model
─────────────────
* Audio chunks arrive on the WebSocket receive coroutine.
* A background asyncio task (_process_loop) fires every PROCESS_INTERVAL.
* A flag (_is_processing) prevents overlapping pipeline runs.
* CPU-bound STT and translation are offloaded to a thread-pool executor so
  the event loop stays free.
* TTS uses asyncio.create_subprocess_exec — inherently non-blocking.
"""

import asyncio
import base64
import json
import logging
import time
from typing import Any, Dict

import numpy as np

logger = logging.getLogger(__name__)

# ── Tuning constants ─────────────────────────────────────────────────────────

# Minimum audio to run STT at all (seconds @ 16 kHz)
MIN_AUDIO_SEC = 1.5
MIN_SAMPLES = int(16_000 * MIN_AUDIO_SEC)

# Hard cap: force-flush even during continuous speech
MAX_AUDIO_SEC = 6.0
MAX_SAMPLES = int(16_000 * MAX_AUDIO_SEC)

# VAD state-machine check rate
PROCESS_INTERVAL = 0.5  # seconds

# Audio energy thresholds
# Typical laptop mic: ambient ~0.0002, speech ~0.002–0.2
SILENCE_RMS = 0.0008   # below this = definitely silent
SPEECH_RMS  = 0.0015   # above this = active speech (hysteresis gap prevents flicker)

# How long silence must last after speech before we call it an utterance end
UTTERANCE_END_SILENCE = 0.8  # seconds

# Don't translate unless the transcript has at least this many words
MIN_TRANSLATE_WORDS = 3


# --------------------------------------------------
# Full STT->NMT->TTS pipeline manager for one WebSocket session
# --------------------------------------------------
class StreamController:
    """
    Manages the full STT → Translation → TTS pipeline for one WebSocket
    session.  Instantiate one per connection.

    Parameters
    ----------
    websocket : fastapi.WebSocket
        The accepted WebSocket for this session.
    models : dict
        Must contain keys ``"stt"``, ``"translator"``, ``"tts"``.
    """

    # --------------------------------------------------
    # Init per-session state: websocket, models, lang codes, audio buffer, VAD flags
    # Flow:
    #   websocket + models
    #     ||
    #   Initialize all state fields
    #     ||
    #   StreamController ready for run()
    # --------------------------------------------------
    def __init__(self, websocket, models: Dict[str, Any]):
        self._ws = websocket
        self._models = models
        self.source_lang: str = "hi"
        self.target_lang: str = "en"

        # Audio accumulation buffer (list of Float32 arrays)
        self._chunks: list[np.ndarray] = []

        # Last full transcript we successfully translated (for delta logic)
        self._last_transcript: str = ""

        # Mutex-style flag to avoid concurrent pipeline runs
        self._is_processing: bool = False
        self._running: bool = False

        # VAD state — track speech/silence transitions
        self._speech_active: bool = False        # True while RMS >= SPEECH_RMS
        self._silence_since: float | None = None # perf_counter when silence began

    # ------------------------------------------------------------------ #
    #  Entry point                                                         #
    # ------------------------------------------------------------------ #

    # --------------------------------------------------
    # WebSocket session loop — receive audio/control, spawn pipeline background task
    # Flow:
    #   Client Connect
    #     ||
    #   create_task(_process_loop)
    #     ||
    #   Binary PCM -> _chunks / Text -> _handle_control
    #     ||
    #   Session end
    # --------------------------------------------------
    async def run(self) -> None:
        """Main loop — awaited for the lifetime of the WebSocket session."""
        self._running = True
        bg = asyncio.create_task(self._process_loop(), name="pipeline-loop")
        try:
            while self._running:
                try:
                    msg = await asyncio.wait_for(
                        self._ws.receive(), timeout=30.0
                    )
                except asyncio.TimeoutError:
                    # Keep-alive ping — nothing to do
                    continue

                if msg.get("type") == "websocket.disconnect":
                    logger.info("Client disconnected gracefully.")
                    break

                if "text" in msg:
                    await self._handle_control(msg["text"])

                elif "bytes" in msg and msg["bytes"]:
                    pcm = np.frombuffer(msg["bytes"], dtype=np.float32).copy()
                    self._chunks.append(pcm)

        except Exception:
            logger.exception("Session error")
        finally:
            self._running = False
            bg.cancel()
            try:
                await bg
            except asyncio.CancelledError:
                pass
            logger.info("Stream session ended.")

    # ------------------------------------------------------------------ #
    #  Control message handler                                             #
    # ------------------------------------------------------------------ #

    # --------------------------------------------------
    # Parse JSON control message and dispatch: start/stop/flush/ping
    # Flow:
    #   JSON text
    #     ||
    #   json.loads -> action
    #     ||
    #   Dispatch to state update or _process_buffer
    # --------------------------------------------------
    async def _handle_control(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Malformed control JSON: %r", raw[:120])
            return

        action = data.get("action")

        if action == "start":
            self.source_lang = data.get("source_lang", "hi")
            self.target_lang = data.get("target_lang", "en")
            self._chunks.clear()
            self._last_transcript = ""
            logger.info(
                "Session started: %s → %s", self.source_lang, self.target_lang
            )
            await self._send({"type": "status", "message": "listening"})

        elif action == "stop":
            logger.info("Client requested stop.")
            self._running = False

        elif action == "flush":
            # Client signals pause / end of utterance — process immediately
            await self._process_buffer()

        elif action == "ping":
            await self._send({"type": "pong"})

    # ------------------------------------------------------------------ #
    #  Background processing loop                                          #
    # ------------------------------------------------------------------ #

    # --------------------------------------------------
    # Background VAD task — detect speech/silence, flush buffer on utterance end
    # Flow:
    #   Every 0.5s
    #     ||
    #   Compute RMS of recent audio
    #     ||
    #   VAD state machine
    #     ||
    #   Trigger _process_buffer on utterance end
    # --------------------------------------------------
    async def _process_loop(self) -> None:
        """
        VAD state machine — fires every PROCESS_INTERVAL seconds.

        States
        ------
        idle      : no speech detected yet  → trim buffer to 0.5 s context
        speech    : RMS >= SPEECH_RMS        → keep accumulating
        countdown : speech ended, waiting for UTTERANCE_END_SILENCE
                    → if silence holds long enough, flush to pipeline
        """
        while self._running:
            await asyncio.sleep(PROCESS_INTERVAL)

            if self._is_processing or not self._chunks:
                continue

            total_samples = sum(len(c) for c in self._chunks)
            total_sec = total_samples / 16_000

            # RMS of the most recent 0.5 s of accumulated audio
            all_audio = np.concatenate(self._chunks)
            recent   = all_audio[-8_000:]   # 0.5 s @ 16 kHz
            rms      = float(np.sqrt(np.mean(recent ** 2)))
            now      = time.perf_counter()

            if rms >= SPEECH_RMS:
                # ── Active speech ───────────────────────────────────────
                if not self._speech_active:
                    logger.info(
                        "[VAD] speech START  RMS=%.4f  buffer=%.2fs",
                        rms, total_sec,
                    )
                self._speech_active = True
                self._silence_since = None

                # Force flush if buffer overflows (very long speech)
                if total_sec >= MAX_AUDIO_SEC:
                    logger.info("[VAD] buffer full (%.2fs) — force flush", total_sec)
                    await self._process_buffer()

            elif self._speech_active:
                # ── Silence after speech — countdown ────────────────────
                if self._silence_since is None:
                    self._silence_since = now
                    logger.info(
                        "[VAD] speech END (silence started)  RMS=%.4f  buffer=%.2fs",
                        rms, total_sec,
                    )

                silence_dur = now - self._silence_since

                if silence_dur >= UTTERANCE_END_SILENCE:
                    logger.info(
                        "[VAD] utterance DONE  silence=%.2fs  buffer=%.2fs → flush",
                        silence_dur, total_sec,
                    )
                    self._speech_active = False
                    self._silence_since = None
                    if total_sec >= MIN_AUDIO_SEC:
                        await self._process_buffer()
                    else:
                        logger.info("[VAD] utterance too short (%.2fs) — discard", total_sec)
                        self._chunks.clear()

            else:
                # ── Idle silence — keep only a 0.5 s context tail ───────
                if total_samples > 8_000:
                    self._chunks = [all_audio[-8_000:]]

    # ------------------------------------------------------------------ #
    #  Core pipeline                                                       #
    # ------------------------------------------------------------------ #

    # --------------------------------------------------
    # Core pipeline: STT -> guards -> delta dedup -> NMT -> TTS -> send
    # Flow:
    #   Audio Buffer
    #     ||
    #   STT (thread pool)
    #     ||
    #   Hallucination Guards A/B/C/D
    #     ||
    #   Delta dedup
    #     ||
    #   NMT (thread pool) -> TTS -> WebSocket send
    # --------------------------------------------------
    async def _process_buffer(self) -> None:
        """Run STT → Translation → TTS on the current audio buffer."""
        if self._is_processing or not self._chunks:
            return

        # Snapshot and clear atomically (list ops are GIL-protected)
        snapshot = self._chunks.copy()
        self._chunks.clear()

        total_samples = sum(len(c) for c in snapshot)
        if total_samples < MIN_SAMPLES:
            # Put back — not enough audio yet
            self._chunks = snapshot + self._chunks
            return

        self._is_processing = True
        t_pipeline = time.perf_counter()
        try:
            audio = np.concatenate(snapshot)
            audio_sec = len(audio) / 16_000

            # Cap to avoid runaway buffers
            if len(audio) > MAX_SAMPLES:
                audio = audio[-MAX_SAMPLES:]

            # Skip silent chunks
            rms = float(np.sqrt(np.mean(audio ** 2)))
            logger.info(
                "[PIPELINE] start  audio=%.2fs  samples=%d  RMS=%.4f",
                audio_sec, len(audio), rms,
            )
            if rms < SILENCE_RMS:
                logger.info("[PIPELINE] skip — silent (RMS=%.4f < %.4f)", rms, SILENCE_RMS)
                # Keep last 1s as a sliding context window — drop older silence
                tail = audio[-16_000:]
                self._chunks.insert(0, tail)
                return

            await self._send({"type": "status", "message": "processing"})

            # ── 1. STT ──────────────────────────────────────────────────
            loop = asyncio.get_event_loop()
            logger.info("[STT] submitting %.2fs of audio …", audio_sec)
            t_stt = time.perf_counter()
            transcript: str = await loop.run_in_executor(
                None,
                self._models["stt"].transcribe_pcm,
                audio,
                self.source_lang,
            )
            stt_ms = (time.perf_counter() - t_stt) * 1000
            logger.info("[STT] done in %.0fms → %r", stt_ms, transcript[:80] if transcript else "(empty)")

            if not transcript:
                logger.info("[PIPELINE] no transcript — keeping last 1s as context")
                # Keep the tail of the audio as context for the next cycle so
                # speech that started at the end of this window isn't lost.
                tail = audio[-16_000:]
                self._chunks.insert(0, tail)
                await self._send({"type": "status", "message": "listening"})
                return

            # ── 2. Hallucination guards ──────────────────────────────────
            # Guard C runs FIRST — collapse "sentence × N" loops before
            # counting words, so "Are you fine? × 8" (40 words) becomes
            # "Are you fine?" (3 words) and passes Guard A.
            before = transcript
            transcript = self._collapse_repetitions(transcript)
            if transcript != before:
                logger.info(
                    "[GUARD-C] collapsed: %d→%d words  %r → %r",
                    len(before.split()), len(transcript.split()),
                    before[:50], transcript[:50],
                )

            words = transcript.split()

            # Guard A: cap at 40 words (on already-collapsed text)
            if len(words) > 40:
                logger.warning(
                    "[GUARD-A] discarding hallucination (%d words): %r",
                    len(words), transcript[:80],
                )
                await self._send({"type": "status", "message": "listening"})
                return

            # Guard B: repetition-density check
            if len(words) >= 6:
                unique = len({w.lower().strip(".,?!\"'") for w in words})
                ratio  = unique / len(words)
                if ratio < 0.35:
                    logger.warning(
                        "[GUARD-B] discarding repetitive hallucination "
                        "(unique_ratio=%.2f, words=%d): %r", ratio, len(words), transcript[:80]
                    )
                    await self._send({"type": "status", "message": "listening"})
                    return

            # Guard D: minimum word count — skip single-word fragments
            if len(words) < MIN_TRANSLATE_WORDS:
                logger.info(
                    "[GUARD-D] too short (%d word(s)): %r",
                    len(words), transcript,
                )
                await self._send({"type": "status", "message": "listening"})
                return

            # ── 3. Skip if nothing changed since last cycle ───────────────
            if transcript.lower().strip() == self._last_transcript.lower().strip():
                logger.info("[PIPELINE] transcript unchanged — skip translate")
                await self._send({"type": "status", "message": "listening"})
                return

            # ── 4. Delta deduplication ───────────────────────────────────
            new_text = self._extract_new_text(self._last_transcript, transcript)
            self._last_transcript = transcript
            logger.info("[DELTA] new_text=%r", new_text[:80] if new_text else "(none)")

            await self._send({"type": "transcript", "text": transcript})

            if not new_text:
                await self._send({"type": "status", "message": "listening"})
                return

            # ── 5. Translation ───────────────────────────────────────────
            logger.info("[NMT] translating %r (%s→%s) …", new_text[:60], self.source_lang, self.target_lang)
            t_nmt = time.perf_counter()
            translation: str = await loop.run_in_executor(
                None,
                self._models["translator"].translate,
                new_text,
                self.source_lang,
                self.target_lang,
            )
            nmt_ms = (time.perf_counter() - t_nmt) * 1000
            logger.info("[NMT] done in %.0fms → %r", nmt_ms, translation[:80] if translation else "(empty)")

            if not translation:
                return

            await self._send({"type": "translation", "text": translation})

            # ── 6. TTS ───────────────────────────────────────────────────
            logger.info("[TTS] synthesizing %r (lang=%s) …", translation[:60], self.target_lang)
            t_tts = time.perf_counter()
            try:
                wav_bytes: bytes = await self._models["tts"].synthesize(
                    translation, self.target_lang
                )
                tts_ms = (time.perf_counter() - t_tts) * 1000
                logger.info("[TTS] done in %.0fms  wav=%d bytes", tts_ms, len(wav_bytes) if wav_bytes else 0)
                if wav_bytes:
                    b64 = base64.b64encode(wav_bytes).decode("utf-8")
                    await self._send({"type": "audio", "data": b64})
            except Exception:
                tts_ms = (time.perf_counter() - t_tts) * 1000
                logger.exception("[TTS] failed after %.0fms — sending text-only", tts_ms)

            total_ms = (time.perf_counter() - t_pipeline) * 1000
            logger.info(
                "[PIPELINE] done  total=%.0fms  (STT=%.0f NMT=%.0f TTS=%.0f)",
                total_ms, stt_ms, nmt_ms, tts_ms,
            )
            await self._send({"type": "status", "message": "listening"})

        except Exception:
            logger.exception("Pipeline error")
            await self._send(
                {"type": "error", "message": "Internal pipeline error"}
            )
        finally:
            self._is_processing = False

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    # --------------------------------------------------
    # Safe fire-and-forget WebSocket JSON send
    # Flow:
    #   data dict
    #     ||
    #   ws.send_json
    #     ||
    #   On error: set _running=False
    # --------------------------------------------------
    async def _send(self, data: dict) -> None:
        """Safe fire-and-forget WebSocket send."""
        try:
            await self._ws.send_json(data)
        except Exception as exc:
            logger.warning("WebSocket send failed: %s", exc)
            self._running = False

    # --------------------------------------------------
    # Collapse Whisper loop-hallucinations (Guard C)
    # Flow:
    #   Raw transcript
    #     ||
    #   Detect word/phrase repetition (Pass1+Pass2)
    #     ||
    #   De-looped text
    # --------------------------------------------------
    @staticmethod
    def _collapse_repetitions(text: str) -> str:
        """
        Collapse Whisper hallucination loops — both full-string and
        suffix-only repeating patterns.

        Examples
        --------
        "How are you? How are you? How are you?"      →  "How are you?"
        "Are you listening listening listening ..."    →  "Are you listening"
        "Hello Hello Hello"                            →  "Hello"
        "foo bar foo bar foo bar"                      →  "foo bar"
        """
        words = text.split()
        n = len(words)
        if n < 4:
            return text

        def _is_repeating(seq: list, unit_len: int, min_reps: int = 2) -> bool:
            """Return True if seq is unit_len-length unit repeated ≥ min_reps times."""
            unit = seq[:unit_len]
            reps = 0
            for i in range(0, len(seq), unit_len):
                chunk = seq[i : i + unit_len]
                if chunk != unit[: len(chunk)]:
                    return False
                reps += 1
            return reps >= min_reps

        # Pass 1 — full-string repetition (original logic)
        for ul in range(1, n // 2 + 1):
            if _is_repeating(words, ul):
                return " ".join(words[:ul])

        # Pass 2 — suffix repetition
        # Walk through possible "real prefix" lengths; if everything after is
        # a loop of some unit, chop it off.
        for prefix_end in range(1, n - 3):
            suffix = words[prefix_end:]
            m = len(suffix)
            for ul in range(1, m // 2 + 1):
                if _is_repeating(suffix, ul, min_reps=3):
                    # Keep real prefix + one copy of the repeated unit
                    return " ".join(words[:prefix_end] + suffix[:ul])

        return text

    # --------------------------------------------------
    # Return only new words not already in previous transcript (delta dedup)
    # Flow:
    #   previous + current transcript
    #     ||
    #   Word-level suffix/prefix overlap detection
    #     ||
    #   New words only
    # --------------------------------------------------
    @staticmethod
    def _extract_new_text(previous: str, current: str) -> str:
        """
        Return only the genuinely *new* words in *current* that aren't
        already present at the end of *previous*.

        This prevents re-translating the same partial sentence on every
        processing cycle.
        """
        if not previous:
            return current

        prev_words = previous.lower().split()
        curr_words = current.split()

        # Find the longest suffix of previous that is a prefix of current
        overlap = 0
        for i in range(min(len(prev_words), len(curr_words)), 0, -1):
            if prev_words[-i:] == [w.lower() for w in curr_words[:i]]:
                overlap = i
                break

        if overlap:
            tail = curr_words[overlap:]
            return " ".join(tail).strip()

        # No overlap — treat entire current text as new
        return current
