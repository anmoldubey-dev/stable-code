# ================================================================
# FILE EXECUTION FLOW
# ================================================================
#
# [ START ]
#    |
#    v
# +-------------------------------+
# | load()                        |
# | * download and init models    |
# +-------------------------------+
#    |
#    |----> <ParlerTTSForConditionalGeneration> -> from_pretrained()
#    |      * load main TTS model
#    |
#    |----> <AutoTokenizer> -> from_pretrained()
#    |      * load Indic prompt tokenizer
#    |
#    |----> <AutoTokenizer> -> from_pretrained()
#    |      * load T5 description tokenizer
#
#    |
#    v
# +-------------------------------+
# | generate()                    |
# | * full TTS synthesis pipeline |
# +-------------------------------+
#    |
#    |----> <PersonaManager> -> guard() * language voice guardrail
#    |
#    |----> <PersonaManager> -> get_or_encode()
#    |      * cached description tensor
#    |
#    |----> _split_sentences() * chunk long input text
#    |
#    |----> _generate_chunk() * single sentence inference
#    |         |
#    |         |----> <torch> -> manual_seed() * fix seed 2026
#    |         |
#    |         |----> <model> -> generate() * Parler inference
#    |
#    |----> <np> -> concatenate() * stitch audio chunks
#
# ================================================================

import os
import re
import logging
import numpy as np
import torch

from core.persona_manager import PersonaManager, INFERENCE_SEED

logger = logging.getLogger(__name__)

SAMPLE_RATE = 44100
SILENCE_200MS = np.zeros(int(SAMPLE_RATE * 0.2), dtype=np.float32)


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?।|])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


class TTSEngine:
    def __init__(self, model_name: str, device: str = "cuda"):
        self.model_name = model_name
        self.device = device if torch.cuda.is_available() and device == "cuda" else "cpu"
        self.model = None
        self.description_tokenizer = None
        self.prompt_tokenizer = None
        self.persona = PersonaManager()
        self.ready = False
        self.sample_rate = SAMPLE_RATE

    def load(self):
        from transformers import AutoTokenizer
        from parler_tts import ParlerTTSForConditionalGeneration

        token = os.getenv("HF_TOKEN") or None
        logger.info("Loading model: %s on %s", self.model_name, self.device)

        self.model = ParlerTTSForConditionalGeneration.from_pretrained(
            self.model_name, token=token
        ).to(self.device)
        self.model.eval()

        self.prompt_tokenizer = AutoTokenizer.from_pretrained(self.model_name, token=token)

        try:
            desc_name = self.model.config.text_encoder._name_or_path
            logger.info("Description tokenizer: %s", desc_name)
            self.description_tokenizer = AutoTokenizer.from_pretrained(desc_name, token=token)
        except Exception as e:
            logger.warning("Falling back to single tokenizer (%s)", e)
            self.description_tokenizer = self.prompt_tokenizer

        logger.info(
            "Tokenizers — description: %s | prompt: %s",
            type(self.description_tokenizer).__name__,
            type(self.prompt_tokenizer).__name__,
        )

        global SAMPLE_RATE, SILENCE_200MS
        SAMPLE_RATE = self.model.audio_encoder.config.sampling_rate
        SILENCE_200MS = np.zeros(int(SAMPLE_RATE * 0.2), dtype=np.float32)
        self.sample_rate = SAMPLE_RATE
        logger.info("Audio sample rate: %d Hz", SAMPLE_RATE)

        self.ready = True
        logger.info("Model ready.")

    def _generate_chunk(
        self,
        text: str,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
    ) -> np.ndarray:
        prompt_inputs = self.prompt_tokenizer(
            text, return_tensors="pt"
        ).to(self.device)

        torch.manual_seed(INFERENCE_SEED)

        with torch.no_grad():
            generation = self.model.generate(
                input_ids=input_ids,
                attention_mask=attention_mask,
                prompt_input_ids=prompt_inputs.input_ids,
                prompt_attention_mask=prompt_inputs.attention_mask,
                do_sample=True,
                temperature=1.1,
                min_new_tokens=10,
                max_new_tokens=5200,
            )

        audio = generation.cpu().numpy().squeeze().astype(np.float32)
        peak = np.abs(audio).max()
        if peak > 0:
            audio = audio / peak * 0.95
        return audio

    def generate(
        self,
        text: str,
        voice_name: str,
        emotion: str,
        language: str = "",
        max_length: int = 300,
    ) -> np.ndarray:
        voice_name = self.persona.guard(voice_name, language)

        input_ids, attention_mask, description = self.persona.get_or_encode(
            voice_name, emotion, language,
            self.description_tokenizer, self.device,
        )
        logger.info("Description: %s", description)

        chunks = [text] if len(text) <= max_length else (_split_sentences(text) or [text])

        audio_parts: list[np.ndarray] = []
        for chunk in chunks:
            try:
                part = self._generate_chunk(chunk, input_ids, attention_mask)
            except RuntimeError as exc:
                if "out of memory" in str(exc).lower() and self.device == "cuda":
                    logger.warning("CUDA OOM — falling back to CPU.")
                    torch.cuda.empty_cache()
                    self.model = self.model.cpu()
                    self.persona.clear_cache()
                    self.device = "cpu"
                    input_ids = input_ids.cpu()
                    attention_mask = attention_mask.cpu()
                    part = self._generate_chunk(chunk, input_ids, attention_mask)
                else:
                    raise
            audio_parts.append(part)
            if len(chunks) > 1:
                audio_parts.append(SILENCE_200MS.copy())

        if audio_parts:
            if len(chunks) > 1:
                audio_parts = audio_parts[:-1]
            return np.concatenate(audio_parts)
        return np.zeros(SAMPLE_RATE, dtype=np.float32)
