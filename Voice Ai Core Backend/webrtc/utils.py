# ================================================================
# FILE EXECUTION FLOW
# ================================================================
#
# [ START ]
#     |
#     v
# +----------------------------------+
# | wav_bytes_to_pcm()               |
# | * parse WAV bytes to float32     |
# +----------------------------------+
#     |
#     |----> <wave> -> open()             * read WAV header and frames
#     |
#     |----> <np> -> frombuffer()         * decode int16 or float32 samples
#     |
#     |----> reshape() / mean()           * stereo or multi-channel to mono
#     |
#     v
# [ RETURN (pcm_float32, sample_rate) ]
#     |
#     v
# +----------------------------------+
# | resample_audio()                 |
# | * polyphase resample audio       |
# +----------------------------------+
#     |
#     |----> <math> -> gcd()             * compute up/down ratio factors
#     |
#     |----> <signal> -> resample_poly() * high-quality polyphase resample
#     |
#     v
# [ RETURN float32 array at to_sr ]
#     |
#     v
# +----------------------------------+
# | float32_to_int16()               |
# | * float32 to int16 PCM           |
# +----------------------------------+
#     |
#     |----> <np> -> clip()              * clamp values to [-1.0, 1.0]
#     |
#     |----> astype()                    * scale and cast to int16
#     |
#     v
# [ RETURN int16 array ]
#     |
#     v
# +----------------------------------+
# | int16_to_float32()               |
# | * int16 to float32 PCM           |
# +----------------------------------+
#     |
#     |----> astype() / divide          * normalize int16 to float32 range
#     |
#     v
# [ RETURN float32 array ]
#     |
#     v
# +----------------------------------+
# | webrtc_time_base()               |
# | * return standard WebRTC base    |
# +----------------------------------+
#     |
#     v
# [ RETURN Fraction(1, 48000) ]
#
# ================================================================

import io
import wave
from fractions import Fraction
from math import gcd
from typing import Tuple

import numpy as np
from scipy.signal import resample_poly


def wav_bytes_to_pcm(wav_bytes: bytes) -> Tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(wav_bytes)) as wf:
        n_channels   = wf.getnchannels()
        sample_rate  = wf.getframerate()
        sample_width = wf.getsampwidth()
        n_frames     = wf.getnframes()
        raw          = wf.readframes(n_frames)

    if sample_width == 2:
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_width == 4:
        pcm = np.frombuffer(raw, dtype=np.float32).copy()
    else:
        raise ValueError(
            f"Unsupported WAV sample width: {sample_width} bytes "
            f"(expected 2 for int16 or 4 for float32)"
        )

    if n_channels == 2:
        pcm = pcm.reshape(-1, 2).mean(axis=1).astype(np.float32)
    elif n_channels > 2:
        pcm = pcm.reshape(-1, n_channels).mean(axis=1).astype(np.float32)

    return pcm, sample_rate


def resample_audio(pcm: np.ndarray, from_sr: int, to_sr: int) -> np.ndarray:
    if from_sr == to_sr:
        return pcm.astype(np.float32)

    g    = gcd(from_sr, to_sr)
    up   = to_sr   // g
    down = from_sr // g
    resampled = resample_poly(pcm.astype(np.float64), up, down)
    return resampled.astype(np.float32)


def float32_to_int16(pcm: np.ndarray) -> np.ndarray:
    clipped = np.clip(pcm, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16)


def int16_to_float32(pcm: np.ndarray) -> np.ndarray:
    return pcm.astype(np.float32) / 32768.0


def webrtc_time_base() -> Fraction:
    return Fraction(1, 48_000)
