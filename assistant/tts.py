"""Text-to-speech: Kokoro-82M (ONNX, CPU) with Piper as fallback. Fully local."""
from __future__ import annotations

import asyncio
import logging
import re

import numpy as np

from .config import MODELS_DIR

log = logging.getLogger("tts")

KOKORO_DIR = MODELS_DIR / "kokoro"
PIPER_DIR = MODELS_DIR / "piper"


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s*>=\s*", " version ", text)
    text = re.sub(r"(?m)^\s*(\d+\.|[-•])\s+", "", text)  # list markers
    text = re.sub(r"[*_`#>]+", "", text)  # never read markdown aloud
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]+", "", text)  # or emoji
    parts = re.split(r"(?<=[.!?…])\s+|\n+", text.strip())
    out: list[str] = []
    for p in (p.strip() for p in parts):
        if not p:
            continue
        # merge very short fragments so prosody stays natural
        if out and len(out[-1]) < 25:
            out[-1] = f"{out[-1]} {p}"
        else:
            out.append(p)
    return out


class TTS:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.engine = cfg.get("engine", "kokoro")
        self._kokoro = None
        self._piper = None

    def load(self):
        if self.engine == "kokoro":
            try:
                from kokoro_onnx import Kokoro

                self._kokoro = Kokoro(str(KOKORO_DIR / "kokoro-v1.0.onnx"), str(KOKORO_DIR / "voices-v1.0.bin"))
                log.info("kokoro ready (voice %s)", self.cfg.get("kokoro_voice"))
                return
            except Exception as e:
                log.warning("kokoro unavailable (%s); falling back to piper", e)
                self.engine = "piper"
        from piper import PiperVoice

        self._piper = PiperVoice.load(str(PIPER_DIR / f"{self.cfg['piper_voice']}.onnx"))
        log.info("piper ready (voice %s)", self.cfg["piper_voice"])

    def _synth(self, text: str) -> tuple[np.ndarray, int]:
        if self._kokoro is None and self._piper is None:
            self.load()
        if self._kokoro is not None:
            voice = self.cfg.get("kokoro_voice", "af_heart")
            lang = "ja" if voice.startswith("j") else ("en-gb" if voice.startswith("b") else "en-us")
            audio, sr = self._kokoro.create(text, voice=voice, speed=self.cfg.get("kokoro_speed", 1.0), lang=lang)
            return audio.astype(np.float32), sr
        chunks = list(self._piper.synthesize(text))
        if not chunks:
            return np.zeros(1, np.float32), 22050
        return np.concatenate([c.audio_float_array for c in chunks]).astype(np.float32), chunks[0].sample_rate

    async def synth(self, text: str) -> tuple[np.ndarray, int]:
        return await asyncio.to_thread(self._synth, text)
