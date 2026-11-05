"""The second, hinted Whisper look at a short scrap of sound."""
import numpy as np
import pytest

from assistant import audio, wakeword
from assistant.wakeword import WakeWord


@pytest.mark.parametrize("heard", ["Emil?", "Ré 1000", "Miu", "Me", "Hey", "Oh", "Okay.", "Leo.", "Neo"])
def test_scraps_that_could_be_her_name_get_a_second_look(heard):
    assert wakeword.could_be_her_name(heard)


@pytest.mark.parametrize("heard", ["", "Thank you.", "Yeah", "push-ups", "What's that?", "Right.", "Let's go"])
def test_scraps_that_cannot_be_her_name_do_not(heard):
    assert not wakeword.could_be_her_name(heard)


def _run(monkeypatch, first_look: str) -> tuple[list, list]:
    """One short utterance through the real wake loop: (hinted looks, wakes)."""
    blocks = {"n": 0}

    class Vad:
        def __init__(self, _t):
            pass

        def is_speech(self, _b):
            return 2 <= blocks["n"] < 14

        def reset(self):
            pass

    hinted, wakes = [], []
    w = WakeWord({}, on_wake=lambda: wakes.append(1), is_busy=lambda: False, verify=lambda _a: first_look,
                 verify_hinted=lambda _a: hinted.append(1) or "Hey Mio")

    class Mic:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self, n):
            blocks["n"] += 1
            if blocks["n"] > 60:
                w._stop.set()
            return np.zeros((n, 1), np.int16), False

    monkeypatch.setattr(audio, "_Vad", Vad)
    monkeypatch.setattr(wakeword.sd, "InputStream", lambda **_k: Mic())
    w._listen_whisper()
    return hinted, wakes


def test_a_misheard_name_still_wakes_her(monkeypatch):
    hinted, wakes = _run(monkeypatch, "Emil?")
    assert (len(hinted), len(wakes)) == (1, 1)


def test_chatter_costs_only_one_whisper_look(monkeypatch):
    hinted, wakes = _run(monkeypatch, "Thank you.")
    assert (len(hinted), len(wakes)) == (0, 0)


def test_her_name_needs_no_second_look(monkeypatch):
    hinted, wakes = _run(monkeypatch, "Hey, Mio.")
    assert (len(hinted), len(wakes)) == (0, 1)
