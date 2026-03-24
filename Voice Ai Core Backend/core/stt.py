# ================================================================
# FILE EXECUTION FLOW
# ================================================================
#
# [ START ]
#     |
#     v
# +-----------------------------+
# | stt_sync()                  |
# | * RMS check then transcribe |
# +-----------------------------+
#     |
#     |----> <np> -> sqrt()                  * compute RMS floor check
#     |
#     OR (rms < _RMS_FLOOR → return "")
#     |
#     |----> <StreamingTranscriber> -> transcribe_pcm()  * Whisper decode
#     |
#     v
# +-----------------------------+
# | _collapse_repetitions()     |
# | * remove ngram hallucinations|
# +-----------------------------+
#     |
#     |----> _is_repeating()                 * detect repeating n-gram units
#     |
#     |----> join()                          * collapse to single occurrence
#     |
#     v
# +-----------------------------+
# | _is_hallucination()         |
# | * final transcript quality  |
# +-----------------------------+
#     |
#     |----> _collapse_repetitions()         * clean loops before guarding
#     |
#     |----> len()                           * Guard A: word count check
#     |
#     OR
#     |
#     |----> set()                           * Guard B: unique ratio check
#
# ================================================================

import logging
from typing import Optional

import numpy as np

from backend.core.config import LANGUAGE_CONFIG
from backend.core.state import _m

logger = logging.getLogger("callcenter.stt")

# RMS floor — reject frames below this before sending to Whisper.
# 0.015 filters wind/breath/HVAC while keeping normal speech (typical speech RMS 0.03-0.15).
_RMS_FLOOR = 0.015


def stt_sync(pcm: np.ndarray, lang: str) -> str:
    raw_rms = float(np.sqrt(np.mean(pcm ** 2)))
    if raw_rms < _RMS_FLOOR:
        logger.debug("[STT] skip — below speech floor (rms=%.5f)", raw_rms)
        return ""
    stt_prompt: Optional[str] = LANGUAGE_CONFIG.get(lang, {}).get("stt_prompt")
    # Pass lang directly — transcribe_pcm validates against _ALL_SUPPORTED_LANGS internally,
    # so all 26 model-supported languages (de, pl, bn, gu, kn, pa, etc.) get a proper hint.
    return _m["stt"].transcribe_pcm(pcm, language=lang, initial_prompt=stt_prompt)


def _collapse_repetitions(text: str) -> str:
    words = text.split()
    n = len(words)
    if n < 4:
        return text

    def _is_repeating(seq: list, unit_len: int, min_reps: int = 2) -> bool:
        unit = seq[:unit_len]
        reps = 0
        for i in range(0, len(seq), unit_len):
            if seq[i: i + unit_len] != unit[: len(seq[i: i + unit_len])]:
                return False
            reps += 1
        return reps >= min_reps

    for ul in range(1, n // 2 + 1):
        if _is_repeating(words, ul):
            return " ".join(words[:ul])

    for prefix_end in range(1, n - 3):
        suffix = words[prefix_end:]
        m = len(suffix)
        for ul in range(1, m // 2 + 1):
            if _is_repeating(suffix, ul, min_reps=3):
                return " ".join(words[:prefix_end] + suffix[:ul])

    return text


def _is_hallucination(text: str) -> bool:
    text = _collapse_repetitions(text)
    words = text.split()
    if len(words) > 40:
        logger.warning("[GUARD-A] dropping: too many words (%d): %r", len(words), text[:80])
        return True
    if len(words) >= 6:
        unique = len({w.lower().strip(".,?!\"'") for w in words})
        if unique / len(words) < 0.35:
            logger.warning("[GUARD-B] dropping: repetitive (unique=%.0f%%): %r",
                           unique / len(words) * 100, text[:80])
            return True
    return False
