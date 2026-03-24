# ================================================================
# FILE EXECUTION FLOW
# ================================================================
#
# [ START ]
#    |
#    v
# +-------------------------------+
# | __init__()                    |
# | * init queue audio source     |
# +-------------------------------+
#    |
#    |----> <rtc.AudioSource> -> AudioSource()   * create LiveKit source
#    |
#    v
# +-------------------------------+
# | start()                       |
# | * launch background pump      |
# +-------------------------------+
#    |
#    |----> _pump()                              * schedule coroutine
#    |
#    v
# +-------------------------------+
# | push_tts_wav()                |
# | * decode enqueue WAV chunks   |
# +-------------------------------+
#    |
#    |----> wav_bytes_to_pcm()                   * parse WAV bytes
#    |
#    |----> resample_audio()                     * convert to 48kHz
#    |
#    |----> float32_to_int16()                   * convert sample format
#    |
#    |----> <asyncio.Queue> -> put_nowait()       * enqueue 20ms chunks
#    |
#    v
# +-------------------------------+
# | _pump()                       |
# | * dequeue send to LiveKit     |
# +-------------------------------+
#    |
#    |----> <asyncio.Queue> -> get()             * read next chunk
#    |
#    |----> <rtc.AudioSource> -> capture_frame() * push frame to room
#    |
#    v
# +-------------------------------+
# | clear()                       |
# | * drain queue on barge-in     |
# +-------------------------------+
#    |
#    |----> <asyncio.Queue> -> get_nowait()      * drain all frames
#    |
#    v
# +-------------------------------+
# | stop()                        |
# | * cancel pump task            |
# +-------------------------------+
#
# ================================================================

import asyncio
import logging
from typing import Optional

import numpy as np

from backend.webrtc.utils import wav_bytes_to_pcm, resample_audio, float32_to_int16

logger = logging.getLogger("callcenter.livekit.audio_source")

_SR            = 48_000
_FRAME_SAMPLES = 960
_MAX_QUEUE     = 500


class TtsAudioSource:

    def __init__(self) -> None:
        from livekit import rtc
        self.source: "rtc.AudioSource" = rtc.AudioSource(
            sample_rate=_SR,
            num_channels=1,
        )
        self._queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=_MAX_QUEUE)
        self._closed: bool = False
        self._task:   Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._pump())

    def stop(self) -> None:
        self._closed = True
        if self._task and not self._task.done():
            self._task.cancel()

    async def _pump(self) -> None:
        from livekit import rtc

        while not self._closed:
            try:
                chunk: np.ndarray = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                break

            frame = rtc.AudioFrame(
                data               = chunk.tobytes(),
                sample_rate        = _SR,
                num_channels       = 1,
                samples_per_channel= _FRAME_SAMPLES,
            )
            try:
                await self.source.capture_frame(frame)
            except Exception as exc:
                # Log but keep pumping — a single bad frame shouldn't kill audio
                logger.debug("[TtsAudioSource] capture_frame error (skipped): %s", exc)

    async def push_tts_wav(self, wav_bytes: bytes) -> None:
        if not wav_bytes:
            logger.warning("[TtsAudioSource] push_tts_wav called with empty bytes — skipping")
            return

        loop = asyncio.get_event_loop()

        # Offload blocking WAV decode + scipy resample to a thread so the
        # event loop is not stalled (resample_poly on 24→48 kHz can take
        # hundreds of milliseconds for a full utterance).
        def _decode_and_resample() -> np.ndarray:
            pcm_f32, native_sr = wav_bytes_to_pcm(wav_bytes)
            if native_sr != _SR:
                pcm_f32 = resample_audio(pcm_f32, native_sr, _SR)
            return float32_to_int16(pcm_f32)

        try:
            pcm_i16 = await loop.run_in_executor(None, _decode_and_resample)
        except Exception:
            logger.exception("[TtsAudioSource] Failed to decode/resample WAV bytes — skipping")
            return

        for i in range(0, len(pcm_i16), _FRAME_SAMPLES):
            chunk = pcm_i16[i : i + _FRAME_SAMPLES]

            if len(chunk) < _FRAME_SAMPLES:
                chunk = np.pad(chunk, (0, _FRAME_SAMPLES - len(chunk)))

            if self._queue.full():
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                logger.debug("[TtsAudioSource] queue overflow — oldest frame dropped")

            try:
                self._queue.put_nowait(chunk)
            except asyncio.QueueFull:
                pass

    def clear(self) -> int:
        drained = 0
        while True:
            try:
                self._queue.get_nowait()
                drained += 1
            except asyncio.QueueEmpty:
                break
        if drained:
            logger.debug("[TtsAudioSource] cleared %d frames on barge-in", drained)
        return drained
