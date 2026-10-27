"""Wake word ("Hey Mio"), fully local. Two detectors, picked with `[wakeword] detector`:

- "whisper" (default): Silero VAD finds short utterances (or the first ~1.8 s of a longer one) and
  Whisper transcribes just those; she wakes when her name is in the first words. Tested on the user's
  own voice: Whisper hears it ~90% of the time, often spelled "Miu".
- "kws": sherpa-onnx keyword spotting (tiny streaming model, CPU) with sound-alike spellings, each
  candidate double-checked by Whisper. Cheaper, but it only caught ~40-65% of the user's real "Hey Mio".

While Mio is busy (listening/thinking/speaking) audio is dropped, so she can't wake herself up.
"""
from __future__ import annotations

import collections
import logging
import re
import threading
import unicodedata

import numpy as np
import sounddevice as sd

from .config import MODELS_DIR, RUNTIME

log = logging.getLogger("wakeword")

# how Whisper writes her name in the user's voice (and close relatives); must come within the first words
NAME = r"(mio|miu|myu|miyo|miyu|mieu|meo|meu|mew|mi-?o|me-?o|me you|mi you|me oh|mi oh)"
GREETING = r"(hey|hi|hay|hei|he|okay|ok|oh|a|re|yo|hello)"


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()  # "Ré, miú" → "Re, miu"


def heard_name(text: str, pattern: re.Pattern, bare: bool = False) -> bool:
    """A greeting + her name at the start ("hey miu, …"); with bare=True also her name on its own ("Mio!").
    Bare names are off by default: short scraps of video audio get transcribed as "Mio" surprisingly often."""
    t = _ascii(text.lower())
    words = re.sub(r"[^a-z']+", " ", t.replace("mi-o", "mio").replace("me-o", "meo")).split()  # "ré-mi-o"
    return bool(pattern.search(" ".join(words[:4]))) or (bare and bool(re.fullmatch(NAME, " ".join(words))))
MODEL = MODELS_DIR / "kws" / "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"


class WakeWord:
    def __init__(self, cfg: dict, on_wake, is_busy, verify=None, verify_hinted=None):
        self.cfg = cfg
        self.verify_hinted = verify_hinted  # same, but Whisper is told her name (second look only)
        self.on_wake = on_wake
        self.is_busy = is_busy
        self.verify = verify  # callable(np.ndarray 16 kHz float32) -> transcript, or None
        self.confirm_re = re.compile(cfg.get("confirm_regex", rf"^{GREETING} {NAME}\b"), re.I)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _keywords_file(self) -> str:
        import sentencepiece as spm

        sp = spm.SentencePieceProcessor(model_file=str(MODEL / "bpe.model"))
        lines = []
        for phrase in self.cfg.get("phrases", ["HEY MIO"]):
            toks = " ".join(sp.encode(phrase.upper(), out_type=str))
            lines.append(f"{toks} :{self.cfg.get('boost', 1.5)} #{self.cfg.get('threshold', 0.25)} "
                         f"@{phrase.upper().replace(' ', '_')}")
        path = RUNTIME / "assistant-keywords.txt"
        path.write_text("\n".join(lines) + "\n")
        return str(path)

    def start(self):
        self._thread = threading.Thread(target=self._run, name="wakeword", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        if self.cfg.get("detector", "whisper") == "whisper" and self.verify:
            log.info("listening for her name (VAD + Whisper)")
            self._loop(self._listen_whisper)
            return
        import sherpa_onnx

        p = lambda name: str(MODEL / name)
        kws = sherpa_onnx.KeywordSpotter(
            tokens=p("tokens.txt"),
            encoder=p("encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            decoder=p("decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            joiner=p("joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            num_threads=1, max_active_paths=8, keywords_file=self._keywords_file(),
            keywords_score=self.cfg.get("boost", 1.5), keywords_threshold=self.cfg.get("threshold", 0.25),
            num_trailing_blanks=1, provider="cpu")
        stream = kws.create_stream()
        log.info("listening for %s", ", ".join(self.cfg.get("phrases", ["HEY MIO"])))
        self._loop(lambda: self._listen(kws, stream))

    def _loop(self, listen):
        delay = 2
        while not self._stop.is_set():  # survive audio hiccups (device changes, PipeWire restarts)
            try:
                listen()
                delay = 2
            except sd.PortAudioError as e:
                if self._stop.is_set():
                    break  # shutting down
                log.warning("microphone error (%s), retrying in %ss", e, delay)
                self._stop.wait(delay)
                delay = min(delay * 2, 60)

    def _listen_whisper(self):
        from .audio import BLOCK, SR, _Vad

        vad = _Vad(self.cfg.get("vad_threshold", 0.15))  # very sensitive: quiet voices count, Whisper filters
        pre = collections.deque(maxlen=10)  # ~320 ms before speech starts
        speech: list[np.ndarray] = []
        silent = 0
        checked = False  # one Whisper look per utterance
        quiet = 0
        stale = int(2 * SR / BLOCK)  # Silero's state drifts on a long stream and stops hearing quiet voices
        end_blocks = int(0.35 * SR / BLOCK)
        early = int(1.8 * SR / BLOCK)  # "hey mio what's the time": check the start without waiting
        longest = int(2.6 * SR / BLOCK)
        with sd.InputStream(samplerate=SR, channels=1, dtype="int16", blocksize=BLOCK, latency="high") as mic:
            while not self._stop.is_set():
                block = mic.read(BLOCK)[0][:, 0].copy()
                if self.is_busy():
                    pre.clear(), speech.clear()
                    checked, silent = False, 0
                    vad.reset()
                    continue
                talking = vad.is_speech(block)
                if not speech:
                    pre.append(block)
                    if talking:
                        speech, silent, checked = list(pre), 0, False
                    else:
                        quiet += 1
                        if quiet >= stale:
                            vad.reset()
                            quiet = 0
                    continue
                speech.append(block)
                silent = 0 if talking else silent + 1
                voiced = len(speech) - len(pre) - silent
                done = silent >= end_blocks
                if not checked and ((done and 4 <= voiced <= longest) or voiced >= early):
                    checked = True
                    audio = np.concatenate(speech[: len(pre) + early + 6]).astype(np.float32) / 32768
                    heard = self.verify(audio) or ""
                    if self.verify_hinted and 0 < len(_ascii(heard).split()) <= 2 and not heard_name(heard, self.confirm_re):
                        # "Emil?", "Ré 1000": a word or two is often her name misheard; listen again with her
                        # name as a hint (it still has to come out as greeting + name)
                        heard = self.verify_hinted(audio) or ""
                    if heard_name(heard, self.confirm_re, self.cfg.get("bare_name", False)):
                        log.info("wake word (heard %r)", heard)
                        speech = []
                        pre.clear()
                        vad.reset()
                        self.on_wake()
                        continue
                    near = len(_ascii(heard).split()) <= 2 or re.match(rf"\W*{GREETING}\b", _ascii(heard), re.I)
                    (log.info if near else log.debug)("not her name: %r", heard)  # near misses help tuning
                if done:
                    speech = []
                    pre.clear()
                    vad.reset()
                    quiet = 0

    def _listen(self, kws, stream):
        block = 1600  # 100 ms
        ring = collections.deque(maxlen=25)  # last 2.5 s for the Whisper double-check
        with sd.InputStream(samplerate=16000, channels=1, dtype="float32", blocksize=block) as mic:
            while not self._stop.is_set():
                samples, _ = mic.read(block)
                if self.is_busy():
                    ring.clear()
                    continue  # drop audio while she's talking/listening
                chunk = np.ascontiguousarray(samples[:, 0])
                ring.append(chunk)
                stream.accept_waveform(16000, chunk)
                while kws.is_ready(stream):
                    kws.decode_stream(stream)
                    word = kws.get_result(stream)
                    if not word:
                        continue
                    kws.reset_stream(stream)
                    if self.verify:
                        heard = self.verify(np.concatenate(ring))
                        if not heard_name(heard or "", self.confirm_re):
                            log.debug("candidate %s rejected (heard %r)", word, heard)
                            continue
                        log.info("wake word: %s (heard %r)", word, heard)
                    else:
                        log.info("wake word: %s", word)
                    ring.clear()
                    self.on_wake()
