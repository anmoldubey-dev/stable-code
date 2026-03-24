"""
backend/webrtc/utils.py
──────────────────────────────────────────────────────────────────────────────
Audio utility functions for the WebRTC layer.

Responsibilities:
  wav_bytes_to_pcm   — parse Piper TTS WAV output into float32 numpy array
  resample_audio     — polyphase resampling between arbitrary sample rates
  float32_to_int16   — convert float32 [-1, 1] → int16 for WebRTC frames
  int16_to_float32   — convert int16 → float32 [-1, 1] for processing

Resampling reference points:
  Browser → Backend  : 48 000 Hz → 16 000 Hz  (÷3, exact integer ratio)
  Piper → WebRTC     : 22 050 Hz → 48 000 Hz  (non-integer; scipy handles it)
"""

import io
import wave
from fractions import Fraction
from math import gcd
from typing import Tuple

import numpy as np
from scipy.signal import resample_poly


# ── WAV parsing ───────────────────────────────────────────────────────────────

# --------------------------------------------------
# wav_bytes_to_pcm -> Parse raw WAV bytes into float32 PCM array
#    ||
# wave.open -> read frames -> int16/float32 decode
#    ||
# Stereo -> mono average -> Returns (pcm_float32, sample_rate)
# --------------------------------------------------
def wav_bytes_to_pcm(wav_bytes: bytes) -> Tuple[np.ndarray, int]:
    """
    Parse raw WAV bytes (as produced by _piper_sync) into a float32 PCM array.

    Handles:
      • 16-bit PCM (Piper default for all ONNX models)
      • 32-bit float WAV (uncommon but tolerated)
      • Mono and stereo input (stereo is averaged to mono)

    Returns:
        (pcm_float32, sample_rate)
        pcm_float32  — shape (N,), values in [-1.0, 1.0]
        sample_rate  — native sample rate from WAV header (e.g. 22050)

    Raises:
        ValueError  — if WAV sample width is not 2 or 4 bytes
        wave.Error  — if wav_bytes is not valid WAV data
    """
    with wave.open(io.BytesIO(wav_bytes)) as wf:
        n_channels   = wf.getnchannels()
        sample_rate  = wf.getframerate()
        sample_width = wf.getsampwidth()   # bytes per sample per channel
        n_frames     = wf.getnframes()
        raw          = wf.readframes(n_frames)

    if sample_width == 2:           # 16-bit signed PCM  (Piper default)
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_width == 4:         # 32-bit float WAV
        pcm = np.frombuffer(raw, dtype=np.float32).copy()
    else:
        raise ValueError(
            f"Unsupported WAV sample width: {sample_width} bytes "
            f"(expected 2 for int16 or 4 for float32)"
        )

    # Stereo interleaved → mono (average channels)
    if n_channels == 2:
        pcm = pcm.reshape(-1, 2).mean(axis=1).astype(np.float32)
    elif n_channels > 2:
        # Multi-channel: average all channels
        pcm = pcm.reshape(-1, n_channels).mean(axis=1).astype(np.float32)

    return pcm, sample_rate


# ── Resampling ────────────────────────────────────────────────────────────────

# --------------------------------------------------
# resample_audio -> Polyphase resample float32 PCM between sample rates
#    ||
# GCD(from_sr, to_sr) -> up/down ratio -> scipy resample_poly
#    ||
# Returns float32 array at to_sr (e.g. 48kHz→16kHz or 22050→48kHz)
# --------------------------------------------------
def resample_audio(pcm: np.ndarray, from_sr: int, to_sr: int) -> np.ndarray:
    """
    High-quality polyphase resampling via scipy.signal.resample_poly.

    Uses the GCD of from_sr and to_sr to find the smallest integer up/down
    ratio, giving an exact rational approximation with minimal aliasing.

    Common paths:
      48000 → 16000 : up=1, down=3   (exact ÷3 integer downsample)
      22050 → 48000 : up=320, down=147  (exact rational upsample)
      16000 → 48000 : up=3, down=1

    Args:
        pcm     — float32 mono array
        from_sr — source sample rate (Hz)
        to_sr   — target sample rate (Hz)

    Returns:
        float32 numpy array resampled to to_sr
    """
    if from_sr == to_sr:
        return pcm.astype(np.float32)

    g    = gcd(from_sr, to_sr)
    up   = to_sr   // g
    down = from_sr // g
    resampled = resample_poly(pcm.astype(np.float64), up, down)
    return resampled.astype(np.float32)


# ── PCM format conversion ─────────────────────────────────────────────────────

# --------------------------------------------------
# float32_to_int16 -> Clip [-1,1] float32 and convert to int16 for WebRTC
# --------------------------------------------------
def float32_to_int16(pcm: np.ndarray) -> np.ndarray:
    """
    Clip float32 [-1.0, 1.0] and convert to int16.
    Used before pushing PCM into an aiortc AudioFrame (format='s16').
    """
    clipped = np.clip(pcm, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16)


# --------------------------------------------------
# int16_to_float32 -> Convert int16 PCM to float32 [-1,1] for processing
# --------------------------------------------------
def int16_to_float32(pcm: np.ndarray) -> np.ndarray:
    """
    Convert int16 PCM to float32 [-1.0, 1.0].
    Used when pulling raw data out of an aiortc AudioFrame.
    """
    return pcm.astype(np.float32) / 32768.0


# ── AudioFrame time base ──────────────────────────────────────────────────────

# --------------------------------------------------
# webrtc_time_base -> Return Fraction(1, 48000) standard WebRTC time base
# --------------------------------------------------
def webrtc_time_base() -> Fraction:
    """Return the standard time_base for 48 kHz WebRTC audio frames."""
    return Fraction(1, 48_000)
