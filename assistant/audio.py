"""Microphone capture with Silero-VAD endpointing, and interruptible playback."""
from __future__ import annotations

import asyncio
import collections
import logging
import threading

import numpy as np
import sounddevice as sd

log = logging.getLogger("audio")

SR = 16000
BLOCK = 512  # 32 ms — the window Silero VAD expects at 16 kHz


class _Vad:
    """Silero VAD (ONNX, ~2 MB) with an energy fallback."""

    def __init__(self, threshold: float):
        self.threshold = threshold
        try:
            from pysilero_vad import SileroVoiceActivityDetector

            self._vad = SileroVoiceActivityDetector()
        except Exception as e:  # pragma: no cover - fallback only
            log.warning("Silero VAD unavailable (%s); using energy VAD", e)
            self._vad = None

    def is_speech(self, block: np.ndarray) -> bool:
        if self._vad is None:
            return float(np.sqrt(np.mean((block.astype(np.float32) / 32768) ** 2))) > 0.02
        return self._vad(block.tobytes()) >= self.threshold

    def reset(self):
        if self._vad is not None and hasattr(self._vad, "reset"):
            self._vad.reset()


class Recorder:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.vad = _Vad(cfg.get("vad_threshold", 0.5))
        self._cancel = threading.Event()
        self._finish = threading.Event()

    def cancel(self):
        """Abort recording and discard audio."""
        self._cancel.set()

    def finish(self):
        """Stop recording now and keep what was said (second push-to-talk press)."""
        self._finish.set()

    async def record(self) -> np.ndarray | None:
        """Record until the speaker pauses. Returns float32 mono 16 kHz, or None."""
        return await asyncio.to_thread(self._record_blocking)

    def _record_blocking(self) -> np.ndarray | None:
        self._cancel.clear()
        self._finish.clear()
        self.vad.reset()
        end_blocks = int(self.cfg.get("end_silence_ms", 800) / 1000 * SR / BLOCK)
        no_speech_blocks = int(self.cfg.get("no_speech_timeout_s", 5) * SR / BLOCK)
        max_blocks = int(self.cfg.get("max_record_s", 20) * SR / BLOCK)
        preroll = collections.deque(maxlen=10)  # ~320 ms before speech starts
        frames: list[np.ndarray] = []
        started, silent, n = False, 0, 0

        with sd.InputStream(samplerate=SR, channels=1, dtype="int16", blocksize=BLOCK) as stream:
            while not self._cancel.is_set() and not self._finish.is_set():
                block, _ = stream.read(BLOCK)
                block = block[:, 0].copy()
                n += 1
                speech = self.vad.is_speech(block)
                if not started:
                    preroll.append(block)
                    if speech:
                        started = True
                        frames.extend(preroll)
                    elif n >= no_speech_blocks:
                        log.info("no speech detected")
                        return None
                    continue
                frames.append(block)
                silent = 0 if speech else silent + 1
                if silent >= end_blocks or n >= max_blocks:
                    break
        if self._cancel.is_set() or not frames:
            return None
        return np.concatenate(frames).astype(np.float32) / 32768.0


class Player:
    """Plays float32 audio; can be interrupted from any thread."""

    def __init__(self, volume: float = 1.0):
        self.volume = volume
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def stop(self):
        self._stop.set()

    def play_blocking(self, audio: np.ndarray, sr: int, on_start=None) -> bool:
        """Returns False if interrupted."""
        with self._lock:
            self._stop.clear()
            audio = np.clip(audio * self.volume, -1, 1).astype(np.float32)
            chunk = sr // 20
            with sd.OutputStream(samplerate=sr, channels=1, dtype="float32", latency="low") as out:
                if on_start:
                    on_start()
                for i in range(0, len(audio), chunk):
                    if self._stop.is_set():
                        return False
                    out.write(audio[i : i + chunk])
            return True

    async def play(self, audio: np.ndarray, sr: int, on_start=None) -> bool:
        return await asyncio.to_thread(self.play_blocking, audio, sr, on_start)


def chime(rising: bool = True) -> tuple[np.ndarray, int]:
    sr = 24000
    t = np.linspace(0, 0.09, int(sr * 0.09), endpoint=False)
    f1, f2 = (660, 990) if rising else (990, 660)
    tone = np.concatenate([np.sin(2 * np.pi * f1 * t), np.sin(2 * np.pi * f2 * t)])
    env = np.minimum(1, np.minimum(np.arange(len(tone)), len(tone) - np.arange(len(tone))) / 400)
    return (0.18 * tone * env).astype(np.float32), sr


def envelope(audio: np.ndarray, sr: int, fps: int = 30) -> list[float]:
    """Mouth-open curve (0..1) for lip-sync, one value per frame."""
    hop = sr // fps
    n = len(audio) // hop
    if n == 0:
        return []
    rms = np.sqrt(np.mean(audio[: n * hop].reshape(n, hop) ** 2, axis=1))
    peak = max(float(np.percentile(rms, 95)), 1e-4)
    env = np.clip(rms / peak, 0, 1) ** 0.7
    return [round(float(v), 2) for v in env]
