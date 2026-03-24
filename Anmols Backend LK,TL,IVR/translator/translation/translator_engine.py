# ==========================================================
# APPLICATION FLOW OVERVIEW
# ==========================================================
# 1. TranslatorEngine    -> Offline M2M-100 (418M) neural translation engine
# 2. __init__()          -> Load M2M100Tokenizer + M2M100ForConditionalGeneration
# 3. translate()         -> Tokenize -> beam search (4) -> decoded translation
# 4. is_pair_supported() -> Check target language in tokenizer vocabulary
# 5. _best_device()      -> Detect CUDA or fallback to CPU
#
# PIPELINE FLOW
# text + src_lang + tgt_lang
#    ||
# TranslatorEngine.translate
#    ||
# tokenizer.src_lang = src_lang  ->  tokenizer(text)  ->  encoded tensors
#    ||
# model.generate (4 beams, max_new_tokens=256)  ->  tokenizer.batch_decode
#    ||
# Translated text string  ->  returned to StreamController
# ==========================================================

"""
translator/translation/translator_engine.py
────────────────────────────────────────────
Offline translation engine using Facebook M2M-100 (418M).

License : MIT  (facebook/m2m100_418M on HuggingFace)
Model   : facebook/m2m100_418M  (~1.8 GB, downloaded once to HF cache)

Advantages over Helsinki-NLP MarianMT:
  • Single model handles all language pairs — no per-pair loading
  • Much better quality on conversational / colloquial Hindi
  • Handles partial sentences and STT-induced typos gracefully
  • 100-language many-to-many (trivially extensible)

Supported pairs (same API, no code changes needed in stream_controller):
  • hi → en
  • en → hi
  • any other pair supported by M2M-100
"""

import logging

from transformers import M2M100ForConditionalGeneration, M2M100Tokenizer

logger = logging.getLogger(__name__)

MODEL_NAME = "facebook/m2m100_418M"


# --------------------------------------------------
# Offline M2M-100 (418M) neural translation engine for any language pair
# --------------------------------------------------
class TranslatorEngine:
    """
    Thread-safe M2M-100 translation engine.

    The model is loaded **once** at instantiation and held in memory.
    Call ``translate`` from a thread-pool executor to avoid blocking the
    asyncio event loop.
    """

    # --------------------------------------------------
    # Load M2M100Tokenizer and M2M100ForConditionalGeneration from HuggingFace
    # Flow:
    #   MODEL_NAME
    #     ||
    #   _best_device()
    #     ||
    #   M2M100Tokenizer + M2M100Model
    #     ||
    #   .eval() ready
    # --------------------------------------------------
    def __init__(self):
        self._device = self._best_device()
        logger.info("Loading M2M-100 translation model: %s …", MODEL_NAME)
        self._tokenizer = M2M100Tokenizer.from_pretrained(MODEL_NAME)
        self._model = M2M100ForConditionalGeneration.from_pretrained(MODEL_NAME)
        if self._device == "cuda":
            self._model = self._model.cuda()
        self._model.eval()
        logger.info("M2M-100 translation model ready (%s).", self._device)

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    # --------------------------------------------------
    # Translate text between any two M2M-100 supported languages
    # Flow:
    #   text + src_lang + tgt_lang
    #     ||
    #   Tokenize
    #     ||
    #   M2M-100 beam search (4 beams)
    #     ||
    #   Decoded translation
    # --------------------------------------------------
    def translate(self, text: str, src_lang: str, tgt_lang: str) -> str:
        """
        Translate *text* from *src_lang* to *tgt_lang*.

        Parameters
        ----------
        text     : source text (e.g. Devanagari Hindi or English).
        src_lang : ISO-639-1 source code, e.g. ``'hi'``.
        tgt_lang : ISO-639-1 target code, e.g. ``'en'``.

        Returns
        -------
        str
            Translated text, or empty string on error.
        """
        if not text or not text.strip():
            return ""

        try:
            self._tokenizer.src_lang = src_lang
            encoded = self._tokenizer(
                text.strip(),
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=512,
            )
            if self._device == "cuda":
                encoded = {k: v.cuda() for k, v in encoded.items()}

            generated = self._model.generate(
                **encoded,
                forced_bos_token_id=self._tokenizer.get_lang_id(tgt_lang),
                num_beams=4,
                max_new_tokens=256,
                early_stopping=True,
            )
            return self._tokenizer.batch_decode(
                generated, skip_special_tokens=True
            )[0]

        except Exception:
            logger.exception(
                "Translation failed [%s→%s]: %r", src_lang, tgt_lang, text[:80]
            )
            return ""

    # --------------------------------------------------
    # Check if target language is in M2M-100's vocabulary
    # Flow:
    #   tgt_lang
    #     ||
    #   tokenizer.get_lang_id
    #     ||
    #   True or False
    # --------------------------------------------------
    def is_pair_supported(self, src_lang: str, tgt_lang: str) -> bool:
        """M2M-100 supports all 100 languages — always True for valid codes."""
        try:
            self._tokenizer.get_lang_id(tgt_lang)
            return True
        except KeyError:
            return False

    # ------------------------------------------------------------------ #
    #  Internals                                                           #
    # ------------------------------------------------------------------ #

    # --------------------------------------------------
    # Return 'cuda' if CUDA available, else 'cpu'
    # Flow:
    #   torch.cuda.is_available()
    #     ||
    #   Return 'cuda' or 'cpu'
    # --------------------------------------------------
    @staticmethod
    def _best_device() -> str:
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
        except ImportError:
            pass
        return "cpu"
