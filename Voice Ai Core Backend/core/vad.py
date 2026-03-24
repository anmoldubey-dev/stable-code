# ================================================================
# FILE EXECUTION FLOW
# ================================================================
#
# [ START ]
#     |
#     v
# +-----------------------------+
# | __init__()                  |
# | * zero counters and buffer  |
# +-----------------------------+
#     |
#     v
# +-----------------------------+
# | push()                      |
# | * add PCM, apply 4 VAD gates|
# +-----------------------------+
#     |
#     |----> <np> -> mean()                  * Gate 0: DC offset removal
#     |
#     |----> <np> -> sqrt()                  * Gate 1: RMS threshold check
#     |
#     |----> _is_voice()                     * Gate 3+4: spectral and ZCR
#     |           |
#     |           |----> <np.fft> -> rfft()  * compute frequency spectrum
#     |           |
#     |           |----> <np> -> mean()      * Gate 3: voice-band ratio
#     |           |
#     |           |----> <np> -> mean()      * Gate 4: zero-crossing rate
#     |
#     |----> _is_voice()                     * Gate 2: consecutive frame count
#     |
#     v
# +-----------------------------+
# | ready()                     |
# | * detect utterance complete |
# +-----------------------------+
#     |
#     |  speech_secs >= MIN_SPEECH           * speech duration met
#     |
#     |  silence_secs >= SILENCE_SECS        * trailing silence met
#     |
#     OR
#     |
#     |  total_secs >= MAX_SECS              * hard cap force-flush
#     |
#     v
# +-----------------------------+
# | flush()                     |
# | * return PCM, reset state   |
# +-----------------------------+
#     |
#     |----> <np> -> concatenate()           * join all stored chunks
#     |
#     |----> <stt> -> stt_sync()             * float32 PCM array passed out
#
# ================================================================

from typing import List, Optional

import numpy as np


class AudioBuf:
    SR               = 16_000
    SPEECH_RMS       = 0.009
    SILENCE_RMS      = 0.0015
    SILENCE_SECS     = 0.55
    MIN_SPEECH       = 0.30
    MAX_SECS         = 15.0
    IDLE_TRIM        = 8_000

    MIN_VOICE_FRAMES = 5
    VOICE_BAND_RATIO = 2.5
    MIN_ZCR          = 0.02

    def __init__(self) -> None:
        self._chunks: List[np.ndarray] = []
        self._speech: int = 0
        self._sil:    int = 0
        self._total:  int = 0
        self._active: bool = False
        self._voice_frame_count: int = 0

    def _is_voice(self, pcm: np.ndarray, rms: float) -> bool:
        fft   = np.fft.rfft(pcm)
        freqs = np.fft.rfftfreq(len(pcm), 1.0 / self.SR)
        mag   = np.abs(fft)

        voice_mask = (freqs >= 80) & (freqs <= 4000)
        low_mask   = freqs < 80

        voice_energy = float(np.mean(mag[voice_mask])) if voice_mask.any() else 0.0
        low_energy   = float(np.mean(mag[low_mask]))   if low_mask.any()   else 0.0

        if low_energy > 0 and voice_energy < low_energy * self.VOICE_BAND_RATIO:
            return False

        zcr = float(np.mean(np.abs(np.diff(np.sign(pcm)))))
        return zcr >= self.MIN_ZCR

    def push(self, pcm: np.ndarray) -> None:
        pcm = pcm - np.mean(pcm)
        rms = float(np.sqrt(np.mean(pcm ** 2)))

        is_speech_frame = rms >= self.SPEECH_RMS and self._is_voice(pcm, rms)

        if is_speech_frame:
            self._voice_frame_count += 1
            self._speech += len(pcm)
            self._sil     = 0
            if self._voice_frame_count >= self.MIN_VOICE_FRAMES:
                self._active = True
        else:
            self._voice_frame_count = 0
            if rms < self.SILENCE_RMS and self._active:
                self._sil += len(pcm)
            elif not self._active:
                if self._total + len(pcm) > self.IDLE_TRIM:
                    all_audio    = np.concatenate(self._chunks + [pcm])
                    self._chunks = [all_audio[-self.IDLE_TRIM:]]
                    self._total  = self.IDLE_TRIM
                    self._sil    = 0
                    return

        self._chunks.append(pcm)
        self._total += len(pcm)

    def ready(self) -> bool:
        real_speech = self._speech / self.SR
        silence     = self._sil    / self.SR
        overlong    = self._total  / self.SR >= self.MAX_SECS
        return real_speech >= self.MIN_SPEECH and (silence >= self.SILENCE_SECS or overlong)

    def flush(self) -> Optional[np.ndarray]:
        if not self._chunks:
            return None
        arr = np.concatenate(self._chunks)
        self._chunks            = []
        self._speech            = 0
        self._sil               = 0
        self._total             = 0
        self._active            = False
        self._voice_frame_count = 0
        return arr
