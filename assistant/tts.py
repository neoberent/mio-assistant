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
import time
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
        with self.lock:  # never in the middle of a sentence
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


def _notify(text: str):
    try:
        subprocess.Popen(["notify-send", "-a", "Mio", "-t", "6000", "Mio's voice", text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


class TTS:
    MAX_RETRY_S = 600

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.engine = cfg.get("engine", "kokoro")
        self.retry_s = 60        # how long her real voice rests after it couldn't start (doubles, up to MAX_RETRY_S)
        self._retry_at = None    # when to try it again; None = not waiting for it
        self._retry_thread: threading.Thread | None = None
        self._resting = False    # her real voice stepped aside for a game
        self._kokoro = None
        self._piper = None
        self._chatterbox = None

    def load(self):
        if self.engine == "chatterbox":
            try:
                cb = _Chatterbox(self.cfg)
                cb.start()
                if self._resting:  # a game opened while it was starting
                    cb.close()
                    return
                self._chatterbox = cb
                return
            except Exception as e:
                log.warning("chatterbox unavailable (%s); falling back to kokoro", e)
                _notify("Her usual voice couldn't start, so she's using a backup one for now.")
                self.engine = "kokoro"
                self._retry_at = time.monotonic() + self.retry_s
        if self.engine == "kokoro":
            try:
                self._load_kokoro()
                log.info("kokoro ready (voice %s)", self.cfg.get("kokoro_voice"))
                return
            except Exception as e:
                log.warning("kokoro unavailable (%s); falling back to piper", e)
                self.engine = "piper"
        from piper import PiperVoice

        self._piper = PiperVoice.load(str(PIPER_DIR / f"{self.cfg['piper_voice']}.onnx"))
        log.info("piper ready (voice %s)", self.cfg["piper_voice"])

    def _load_kokoro(self):
        from kokoro_onnx import Kokoro

        self._kokoro = Kokoro(str(KOKORO_DIR / "kokoro-v1.0.onnx"), str(KOKORO_DIR / "voices-v1.0.bin"))

    def step_aside(self):
        """A game is running: give back the real voice's ~2.7 GB of VRAM and ~2.5 GB of RAM. Until come_back() she
        speaks with Kokoro on the CPU, because restarting the real voice takes ~10 s per reply."""
        if self.cfg.get("engine") != "chatterbox":
            return
        self._resting = True
        self._retry_at = None
        if cb := self._chatterbox:
            self._chatterbox = None
            cb.close()
            log.info("chatterbox stepped aside")

    def come_back(self):
        if self.cfg.get("engine") != "chatterbox" or not self._resting:
            return
        self._resting = False
        try:
            cb = _Chatterbox(self.cfg)
            cb.start()
        except Exception as e:  # noqa: BLE001 - e.g. the GPU is still full: the usual retries take it from here
            log.warning("chatterbox didn't come back (%s)", e)
            self._retry_at = time.monotonic() + self.retry_s
            return
        self._chatterbox = cb
        self.engine = "chatterbox"
        self._kokoro = None
        log.info("chatterbox is back after the game")

    def _retry_chatterbox(self):
        """After a failed start (usually the GPU was full), try again beside the sentence being spoken."""
        if self._retry_at is None or time.monotonic() < self._retry_at or \
                (self._retry_thread and self._retry_thread.is_alive()):
            return

        def attempt():
            try:
                cb = _Chatterbox(self.cfg)
                cb.start()
            except Exception as e:  # noqa: BLE001
                self.retry_s = min(self.retry_s * 2, self.MAX_RETRY_S)
                self._retry_at = time.monotonic() + self.retry_s
                log.info("chatterbox still unavailable (%s); next try in %ss", e, self.retry_s)
                return
            self._retry_at = None
            self._chatterbox = cb
            self.engine = "chatterbox"
            log.info("chatterbox is back")
            _notify("Her usual voice is back.")

        self._retry_at = float("inf")  # one attempt at a time; it sets the next time when it's done
        self._retry_thread = threading.Thread(target=attempt, name="chatterbox-retry", daemon=True)
        self._retry_thread.start()

    def _synth(self, text: str) -> tuple[np.ndarray, int]:
        if self._resting:
            if self._kokoro is None:
                self._load_kokoro()
        elif self._kokoro is None and self._piper is None and self._chatterbox is None:
            self.load()
        elif self._chatterbox is None:
            self._retry_chatterbox()
        if (cb := self._chatterbox) is not None:
            try:
                return cb.synth(clean_tags(text))
            except Exception as e:  # one bad sentence: say it with Kokoro, keep Chatterbox for the next
                log.warning("chatterbox failed (%s); kokoro for this sentence", e)
                if self._kokoro is None:
                    self._load_kokoro()
        text = strip_tags(text)
        if (kokoro := self._kokoro) is not None:
            voice = self.cfg.get("kokoro_voice", "af_heart")
            lang = "ja" if voice.startswith("j") else ("en-gb" if voice.startswith("b") else "en-us")
            audio, sr = kokoro.create(text, voice=voice, speed=self.cfg.get("kokoro_speed", 1.0), lang=lang)
            return audio.astype(np.float32), sr
        chunks = list(self._piper.synthesize(text))
        if not chunks:
            return np.zeros(1, np.float32), 22050
        return np.concatenate([c.audio_float_array for c in chunks]).astype(np.float32), chunks[0].sample_rate

    async def synth(self, text: str) -> tuple[np.ndarray, int]:
        return await asyncio.to_thread(self._synth, text)
