"""Speech-to-text with faster-whisper (CTranslate2)."""
from __future__ import annotations

import asyncio
import logging
import re

import numpy as np

from .config import MODELS_DIR

log = logging.getLogger("stt")

# Primes Whisper with words the user says a lot (spelling of names, games, apps). Only for commands, never for
# wake checks, where a prompt makes it hallucinate the words from noise.
VOCAB = ("Hey Mio, open YouTube in Brave. Play Roblox, clip that. Check for updates with paru and pacman on "
         "CachyOS. Niri, Noctalia, kitty, fish, Discord, Steam, Spotify, Hatsune Miku, Wikipedia, GitHub. "
         "Froststrap, Brave Origin, Kate, Trymacs, Jordan & Semih, Rohaze.")

# words Whisper keeps getting wrong for this user → what they said
FIXES = [(r"\b(?:Miu|Meo|Miyo|Mijo)\b", "Mio"), (r"\bPatsune\b", "Hatsune"), (r"\b(?:Road ?blocks?|Roblocks)\b", "Roblox"),
         (r"\b(?:Nearie|Neary|Niri's)\b", "niri"), (r"\bkip that\b", "clip that"), (r"\bPack ?man\b", "pacman"),
         (r"\bParu\b", "paru"), (r"\bCatchy ?OS\b", "CachyOS"), (r"\bYou ?tube\b", "YouTube"),
         (r"\bFrost ?(?:trap|strap)\b", "Froststrap"), (r"\bBrave Origins\b", "Brave Origin"),
         (r"\b(open (?:it|them|this|that) in) Cade\b", r"\1 Kate"), (r"\bTrimax\b", "Trymacs"),
         (r"\b(?:Roe|Row|Ro) ?ha(?:yes|ze|zy|t)\b", "Rohaze"),
         (r"\b(?:all|oh) hot (?=Asian maps)", "Rohaze ")]


class STT:
    def __init__(self, cfg: dict, name: str = ""):
        self.cfg = dict(cfg)
        self.cfg.setdefault("hotwords", name)
        self.names: list[str] = []  # names Mio learned (assistant/learning.py), added to the vocabulary prompt
        self._model = None

    def load(self):
        from faster_whisper import WhisperModel

        device, ctype = self.cfg.get("device", "cuda"), self.cfg.get("compute_type", "int8_float16")
        root = str(MODELS_DIR / "whisper")
        try:
            try:  # offline first; only touch the network if the model isn't downloaded yet
                self._model = WhisperModel(self.cfg["model"], device=device, compute_type=ctype,
                                           download_root=root, local_files_only=True)
            except (OSError, ValueError):
                self._model = WhisperModel(self.cfg["model"], device=device, compute_type=ctype, download_root=root)
        except Exception as e:
            log.warning("whisper on %s failed (%s); falling back to CPU int8", device, e)
            self._model = WhisperModel(self.cfg["model"], device="cpu", compute_type="int8",
                                       download_root=str(MODELS_DIR / "whisper"))
        log.info("whisper %s ready", self.cfg["model"])

    def unload(self):
        """Free the GPU memory (game mode). The next transcription loads it again."""
        if self._model is not None:
            self._model = None
            import gc

            gc.collect()
            log.info("whisper unloaded")

    def _transcribe(self, audio: np.ndarray, hotwords: bool = True, command: bool = False) -> str:
        """Wake checks use hotwords=False (or the name hotword only): fast, greedy, unprimed. command=True is
        a real request: beam search, the vocabulary prompt and the FIXES."""
        if self._model is None:
            self.load()
        segments, _ = self._model.transcribe(
            audio, language=self.cfg.get("language") or None, beam_size=self.cfg.get("beam_size", 5) if command else 1,
            vad_filter=False, condition_on_previous_text=False, without_timestamps=True,
            # helps whisper spell her name right; off for wake checks, where it makes her name up from noise
            hotwords=(self.cfg.get("hotwords") or None) if hotwords and not command else None,
            initial_prompt=self._prompt() if command else None,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        if command:
            for pattern, word in FIXES:
                text = re.sub(pattern, word, text, flags=re.I)
        return text

    def _prompt(self) -> str:
        base = self.cfg.get("vocabulary", VOCAB)
        names = [n for n in self.names if n.lower() not in base.lower()]
        extra = ""
        for n in names:  # Whisper's prompt holds ~220 tokens; keep the learned part to ~400 characters
            if len(extra) + len(n) > 400:
                break
            extra += n + ", "
        return base + (" Also: " + extra.rstrip(", ") + "." if extra else "")

    async def transcribe(self, audio: np.ndarray) -> str:
        return await asyncio.to_thread(self._transcribe, audio, True, True)
