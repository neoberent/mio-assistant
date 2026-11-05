"""If her real voice can't start she speaks with Kokoro for now, and gets her voice back later."""
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from assistant import tts as tts_mod

CHATTERBOX_SR, KOKORO_SR = 24000, 11111


class _Kokoro:
    def __init__(self, *_a):
        pass

    def create(self, *_a, **_k):
        return np.zeros(1, np.float32), KOKORO_SR


@pytest.fixture
def voice(monkeypatch):
    state = SimpleNamespace(ok=False, starts=0, notes=[], gate=threading.Event())
    state.gate.set()

    class Worker:
        def __init__(self, _cfg):
            pass

        def start(self):
            state.starts += 1
            state.gate.wait(5)
            if not state.ok:
                raise RuntimeError("CUDA out of memory")

        def synth(self, _text):
            return np.zeros(1, np.float32), CHATTERBOX_SR

    monkeypatch.setattr(tts_mod, "_Chatterbox", Worker)
    monkeypatch.setitem(sys.modules, "kokoro_onnx", SimpleNamespace(Kokoro=_Kokoro))
    monkeypatch.setattr(tts_mod, "_notify", state.notes.append)
    return state


def _settle(t):
    if t._retry_thread:
        t._retry_thread.join()


def test_a_failed_start_still_speaks_and_tells_the_user_once(voice):
    t = tts_mod.TTS({"engine": "chatterbox"})
    assert t._synth("hi")[1] == KOKORO_SR
    assert t._synth("hi")[1] == KOKORO_SR
    assert len(voice.notes) == 1


def test_no_retry_before_the_wait_is_over(voice):
    t = tts_mod.TTS({"engine": "chatterbox"})
    for _ in range(5):
        t._synth("hi")
    assert voice.starts == 1


def test_her_voice_comes_back_once_it_can_start(voice):
    t = tts_mod.TTS({"engine": "chatterbox"})
    t.retry_s = 0
    t._synth("hi")
    voice.ok = True
    voice.gate.clear()  # the retry is slow to start…
    assert t._synth("hi")[1] == KOKORO_SR  # …and this sentence doesn't wait for it
    voice.gate.set()
    _settle(t)
    assert t._synth("hi")[1] == CHATTERBOX_SR
    assert len(voice.notes) == 2  # fell back, came back


def test_each_failed_retry_waits_longer(voice):
    t = tts_mod.TTS({"engine": "chatterbox"})
    t.retry_s = 1
    t._synth("hi")
    t._retry_at = 0  # the wait is over
    t._synth("hi")
    _settle(t)
    assert voice.starts == 2
    assert t.retry_s == 2


def test_kokoro_as_the_chosen_engine_never_touches_chatterbox(voice):
    t = tts_mod.TTS({"engine": "kokoro"})
    t._synth("hi")
    assert voice.starts == 0
    assert voice.notes == []
