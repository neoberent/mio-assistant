"""Confirmation by voice, 'forget …', and interrupting her while she thinks."""
import asyncio
from types import SimpleNamespace

import pytest

from assistant import daemon
from assistant.daemon import Assistant


@pytest.mark.parametrize("heard", ["Yes.", "yeah", "Yes please.", "Okay, do it.", "Go ahead.", "Sure!", "Yeah, go ahead.",
                                   "Yes, do it please.", "Confirm.", "Yes Mio."])
def test_a_short_clear_answer_confirms(heard):
    assert daemon.voice_says_yes(heard)


@pytest.mark.parametrize("heard", ["", "no", "No, don't.", "yes no", "don't do it", "Thank you.", "stop",
                                   # what a video in the background says, which the mic picks up
                                   "okay guys welcome back", "yeah so today we're going to look at this",
                                   "sure, why not, I mean it was fine", "Yes, I think that's a bad idea."])
def test_anything_else_does_not_confirm(heard):
    assert not daemon.voice_says_yes(heard)


def _with_facts(gone):
    return SimpleNamespace(memory=SimpleNamespace(facts=["x"], forget=lambda _q: gone))


@pytest.mark.parametrize("said", ["forget about it", "forget that timer"])
def test_forget_without_a_matching_fact_goes_on_as_a_normal_request(said):
    assert Assistant._about_me(_with_facts([]), said) == ""


def test_forget_with_a_matching_fact_says_what_went():
    assert "lofi" in Assistant._about_me(_with_facts(["The user likes lofi"]), "forget that I like lofi")


class _Stub:
    def __init__(self):
        self.stopped = self.cancelled = 0

    def stop(self):
        self.stopped += 1

    def cancel(self):
        self.cancelled += 1


def _assistant(monkeypatch):
    monkeypatch.setenv("MIO_TEST", "1")
    a = Assistant.__new__(Assistant)
    a.state, a.gaming, a.dreamer, a.learning, a._last_activity = "idle", False, None, None, 0.0
    a._task, a._trace, a._hints, a.last_src = None, [], [], "none"
    a.cfg = SimpleNamespace(brain={}, avatar={}, gamemode={})
    a.player, a.recorder = _Stub(), _Stub()
    a.spoken, a.states = [], []

    async def set_state(s):
        a.state = s
        a.states.append(s)

    async def nothing(*_a, **_k):
        return None

    async def say(text):
        a.spoken.append(text)

    a.set_state, a.show, a.hide, a.say = set_state, nothing, nothing, say
    a.end_session = lambda: None
    return a


async def _thinking(a):
    """A real request stuck in the brain."""
    forever = asyncio.Event()

    async def respond(_text):
        await forever.wait()

    a.respond = respond
    task = asyncio.create_task(a._interaction("what is the meaning of life"))
    a._task = task
    while a.state != "thinking":
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    return task


async def test_stop_cancels_a_request_she_is_thinking_about(monkeypatch):
    a = _assistant(monkeypatch)
    task = await _thinking(a)
    await a.dispatch({"cmd": "stop"})
    await asyncio.wait([task], timeout=1)
    assert task.cancelled()
    assert a.state == "idle"
    assert a.spoken == []  # no "something went wrong": they asked for this


async def test_push_to_talk_while_thinking_drops_the_request_and_listens(monkeypatch):
    a = _assistant(monkeypatch)
    old = await _thinking(a)
    listening = []

    async def interaction():
        listening.append(a.state)

    a._interaction = interaction
    await a.ptt()
    await asyncio.wait([a._task], timeout=1)
    assert old.cancelled()
    assert listening == ["idle"]  # the old request finished cleaning up before the new one began


async def test_push_to_talk_during_a_confirmation_changes_nothing(monkeypatch):
    a = _assistant(monkeypatch)
    task = await _thinking(a)
    a.state = "confirming"
    await a.ptt()
    assert not task.done()
    assert a._task is task
    task.cancel()


async def test_stop_while_idle_with_a_finished_task_is_harmless(monkeypatch):
    a = _assistant(monkeypatch)

    async def done():
        return None

    a._task = asyncio.create_task(done())
    await a._task
    await a.dispatch({"cmd": "stop"})
    assert a.player.stopped == 1
