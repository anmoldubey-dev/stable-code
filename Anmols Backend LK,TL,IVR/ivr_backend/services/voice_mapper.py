# ==========================================================
# APPLICATION FLOW OVERVIEW
# ==========================================================
# 1. resolve() -> lang + voice_model name -> absolute .onnx path
#
# PIPELINE FLOW
# resolve(lang, voice_model)
#    ||
# VOICE_REGISTRY lookup by lang -> voice_model key
#    ||
# Return absolute path to .onnx model file
# ==========================================================
"""
ivr_backend/services/voice_mapper.py
──────────────────────────────────────
Central voice / language mapping for the IVR backend TTS service.

All 11 supported languages with their Piper voice.
Marathi has no native model → shares Hindi (same Devanagari script).
"""

# ── Language groups (used by LanguageSelector in frontend) ───────────────────
LATIN_GROUP      = ["English", "Spanish", "French"]
DEVANAGARI_GROUP = ["Hindi", "Marathi", "Nepali"]
DRAVIDIAN_GROUP  = ["Telugu", "Malayalam"]
OTHER_GROUP      = ["Russian", "Arabic", "Chinese"]

SUPPORTED_LANGUAGES = LATIN_GROUP + DEVANAGARI_GROUP + DRAVIDIAN_GROUP + OTHER_GROUP

# Legacy aliases kept for backward compat
ENGLISH_GROUP = LATIN_GROUP
HINDI_GROUP   = DEVANAGARI_GROUP

# ── Language display name → Piper model key ──────────────────────────────────
LANGUAGE_TO_MODEL: dict[str, str] = {
    "English":   "en",
    "Spanish":   "es",
    "French":    "fr",
    "Hindi":     "hi",
    "Marathi":   "hi",   # no native mr model — share Hindi (Devanagari)
    "Nepali":    "ne",
    "Telugu":    "te",
    "Malayalam": "ml",
    "Russian":   "ru",
    "Arabic":    "ar",
    "Chinese":   "zh",
}

# ── Language → voice display label (shown in LanguageSelector) ───────────────
LANGUAGE_TO_VOICE_NAME: dict[str, str] = {
    "English":   "Lessac (US)",
    "Spanish":   "Claude (MX)",
    "French":    "Siwis (FR)",
    "Hindi":     "Priyamvada (IN)",
    "Marathi":   "Priyamvada (IN)",
    "Nepali":    "Chitwan (NP)",
    "Telugu":    "Padmavathi (IN)",
    "Malayalam": "Meera (IN)",
    "Russian":   "Irina (RU)",
    "Arabic":    "Kareem (JO)",
    "Chinese":   "Huayan (CN)",
}


def get_voice_name(language: str) -> str:
    """Return display voice name for a language."""
    return LANGUAGE_TO_VOICE_NAME.get(language, "Lessac (US)")


def get_model_key(language: str) -> str:
    """Return Piper model key for a language."""
    return LANGUAGE_TO_MODEL.get(language, "en")
