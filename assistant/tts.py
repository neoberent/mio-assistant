"""Text-to-speech: Chatterbox Turbo (GPU worker in its own venv), Kokoro-82M (ONNX, CPU), Piper. Fully local.
Each engine falls back to the next one if it can't start."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import select
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path

import numpy as np

from .config import MODELS_DIR, XDG_DATA

log = logging.getLogger("tts")

KOKORO_DIR = MODELS_DIR / "kokoro"
PIPER_DIR = MODELS_DIR / "piper"
CHATTERBOX_PY = XDG_DATA / "assistant" / "chatterbox" / ".venv" / "bin" / "python"
WORKER = Path(__file__).with_name("tts_worker.py")
# Sound tags Chatterbox Turbo performs; every other engine (and her speech bubble) drops them.
TAGS = re.compile(r"\s*\[(laugh|chuckle|sigh|gasp|cough|groan|sniff|shush|clear throat)\]\s*", re.I)


def strip_tags(text: str) -> str:
    return re.sub(r"\s{2,}", " ", TAGS.sub(" ", text)).strip()


def clean_tags(text: str) -> str:
    """Keep known tags as [laugh] etc.; remove any other [bracketed] text so it isn't read out."""
    text = re.sub(r"\[(?!(?:laugh|chuckle|sigh|gasp|cough|groan|sniff|shush|clear throat)\])[^\]]*\]", " ", text, flags=re.I)
    return re.sub(r"\s{2,}", " ", text).strip()


def slow_down(audio: np.ndarray, sr: int, tempo: float) -> np.ndarray:
    """Pitch-preserving tempo change with Rubber Band's R3 (finer) engine; ~40 ms per sentence.
    Returns the audio unchanged if rubberband isn't installed or fails."""
    if abs(tempo - 1.0) < 0.01 or not shutil.which("rubberband"):
        return audio
    tmp = Path(tempfile.mkdtemp(prefix="mio-tts-", dir="/dev/shm" if os.path.isdir("/dev/shm") else None))
    try:
        src, dst = tmp / "in.wav", tmp / "out.wav"
        with wave.open(str(src), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(sr)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        r = subprocess.run(["rubberband", "-q", "-3", "-T", str(tempo), str(src), str(dst)], capture_output=True, timeout=20)
        if r.returncode != 0:
            log.warning("rubberband failed: %s", r.stderr.decode(errors="replace")[-200:])
            return audio
        with wave.open(str(dst), "rb") as w:
            raw, width = w.readframes(w.getnframes()), w.getsampwidth()
        if width == 2:
            return np.frombuffer(raw, "<i2").astype(np.float32) / 32768
        if width == 4:  # rubberband writes float WAVs for some inputs
            return np.frombuffer(raw, "<f4").astype(np.float32)
        return audio
    except Exception as e:  # noqa: BLE001 - speed is cosmetic, never lose the sentence over it
        log.warning("slow_down failed (%s)", e)
        return audio
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class _Chatterbox:
    """Talks to tts_worker.py over pipes; one sentence at a time."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.proc = None

    def start(self):
        if not CHATTERBOX_PY.exists():
            raise RuntimeError(f"{CHATTERBOX_PY} missing (run scripts/install_chatterbox.sh)")
        self.proc = subprocess.Popen([str(CHATTERBOX_PY), "-I", str(WORKER), self.cfg.get("chatterbox_device", "cuda"),
                                      "half" if self.cfg.get("chatterbox_half", True) else "full"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None)
        hello = self.proc.stdout.readline()
        if not hello:
            raise RuntimeError(f"worker exited ({self.proc.wait()})")
        log.info("chatterbox ready (%s)", json.loads(hello).get("device"))

    def synth(self, text: str) -> tuple[np.ndarray, int]:
        voice = self.cfg.get("chatterbox_voice") or None
        if voice:
            voice = str(Path(voice).expanduser())
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                log.warning("chatterbox worker not running; restarting")
                self.start()
            try:
                self.proc.stdin.write(json.dumps({"text": text, "voice": voice}).encode() + b"\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._kill()
                raise RuntimeError("chatterbox worker died")
            # a hung worker (stuck CUDA call) would otherwise block here forever and keep her "speaking"
            if not select.select([self.proc.stdout], [], [], float(self.cfg.get("chatterbox_timeout_s", 60)))[0]:
                self._kill()
                raise RuntimeError("chatterbox worker hung; restarted next time")
            head = self.proc.stdout.readline()
            if not head:
                self._kill()
                raise RuntimeError("chatterbox worker died")
            head = json.loads(head)
            if "error" in head:
                raise RuntimeError(head["error"])
            n = head["n"] * 4
            buf = bytearray()
            while len(buf) < n:
                chunk = self.proc.stdout.read(n - len(buf))
                if not chunk:
                    self._kill()
                    raise RuntimeError("chatterbox worker died mid-sentence")
                buf += chunk
            audio = np.frombuffer(bytes(buf), "<f4").copy()
        return slow_down(audio, head["sr"], float(self.cfg.get("chatterbox_tempo", 1.0))), head["sr"]

    def _kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
        self.proc = None

    def close(self):
        self._kill()


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
        self._chatterbox = None

    def load(self):
        if self.engine == "chatterbox":
            try:
                cb = _Chatterbox(self.cfg)
                cb.start()
                self._chatterbox = cb
                return
            except Exception as e:
                log.warning("chatterbox unavailable (%s); falling back to kokoro", e)
                self.engine = "kokoro"
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
        if self._kokoro is None and self._piper is None and self._chatterbox is None:
            self.load()
        if self._chatterbox is not None:
            try:
                return self._chatterbox.synth(clean_tags(text))
            except Exception as e:  # one bad sentence: say it with Kokoro, keep Chatterbox for the next
                log.warning("chatterbox failed (%s); kokoro for this sentence", e)
                if self._kokoro is None:
                    from kokoro_onnx import Kokoro
                    self._kokoro = Kokoro(str(KOKORO_DIR / "kokoro-v1.0.onnx"), str(KOKORO_DIR / "voices-v1.0.bin"))
        text = strip_tags(text)
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
