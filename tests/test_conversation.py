"""Answering her questions without waking her again, requests she quietly dropped, facts you tell her, small talk."""
import asyncio
from types import SimpleNamespace

import pytest

from assistant import clock, daemon, memory, review
from assistant.audio import Player
from assistant.brain import fastpath
from assistant.brain.local import LocalBrain
from assistant.tools import ToolBox
from tests.test_daemon_logic import _assistant
from tests.test_shell_policy import SECURITY

OFFER = "You have 9 updates waiting. Want me to install them?"
EITHER_OR = "Do you want me to close the tab or the whole window?"
OPEN_QUESTION = "Which playlist did you mean?"


@pytest.mark.parametrize("question, heard, want", [
    (OFFER, "Yes.", True),
    (OFFER, "Yes please.", True),
    (OFFER, "No thanks.", True),
    (OFFER, "Nope", True),
    # the mic hears the speakers: a video talking over her offer must not count as a yes
    (OFFER, "okay guys welcome back", False),
    (OFFER, "and that's the whole story", False),
    (EITHER_OR, "the tab", True),
    (OPEN_QUESTION, "my gym one", True),
    (OPEN_QUESTION, "so today we are going to take a look at the new update", False),
    # what Whisper makes of silence and room noise
    (OPEN_QUESTION, "Thank you.", False),
    (OPEN_QUESTION, "you", False),
    (OPEN_QUESTION, "", False),
])
def test_what_counts_as_answering_her(question, heard, want):
    assert daemon.answers_her(question, heard) is want


def _listener(monkeypatch, heard="Yes.", audio=object()):
    a = _assistant(monkeypatch)
    a.cfg.ptt = {}
    a.recordings = 0

    async def record():
        a.recordings += 1
        return audio

    async def transcribe(_audio):
        return heard

    async def beep(_rising):
        return None

    a.recorder.record, a.beep = record, beep
    a.stt = SimpleNamespace(transcribe=transcribe)
    return a


async def test_she_listens_after_asking_a_question(monkeypatch):
    a = _listener(monkeypatch)
    assert await a._hear_answer(OFFER) == "Yes."
    assert "listening" in a.states


async def test_she_does_not_listen_after_a_plain_reply(monkeypatch):
    a = _listener(monkeypatch)
    assert await a._hear_answer("Opening Prism Launcher!") == ""
    assert a.recordings == 0


async def test_she_does_not_listen_during_a_game(monkeypatch):
    """Teammates on voice chat would answer for you."""
    a = _listener(monkeypatch)
    a.gaming = True
    assert await a._hear_answer(OFFER) == ""
    assert a.recordings == 0


async def test_listening_after_questions_can_be_turned_off(monkeypatch):
    a = _listener(monkeypatch)
    a.cfg.ptt = {"listen_after_question": False}
    assert await a._hear_answer(OFFER) == ""
    assert a.recordings == 0


async def test_she_does_not_listen_after_you_stopped_her_mid_question(monkeypatch):
    a = _listener(monkeypatch)
    a.player = Player()
    a.player.stop()
    assert await a._hear_answer(OFFER) == ""
    assert a.recordings == 0


async def test_silence_after_her_question_is_not_an_answer(monkeypatch):
    a = _listener(monkeypatch, audio=None)
    assert await a._hear_answer(OFFER) == ""


async def test_something_that_is_not_an_answer_is_dropped(monkeypatch):
    a = _listener(monkeypatch, heard="okay guys welcome back")
    assert await a._hear_answer(OFFER) == ""


async def _requests(a, replies):
    asked = []
    replies = iter(replies)

    async def respond(text):
        asked.append(text)
        return next(replies)

    a.respond = respond
    return asked


async def test_her_question_answered_by_voice_runs_as_the_next_request(monkeypatch):
    a = _listener(monkeypatch)
    asked = await _requests(a, [OFFER, "Opening a terminal for the update."])
    await a._interaction("Does my PC need any updates?", voice=True)
    assert asked == ["Does my PC need any updates?", "Yes."]
    assert a.spoken == [OFFER, "Opening a terminal for the update."]
    assert a.state == "idle"


async def test_a_typed_request_does_not_open_the_mic(monkeypatch):
    a = _listener(monkeypatch)
    asked = await _requests(a, [OFFER])
    await a._interaction("Does my PC need any updates?")
    assert asked == ["Does my PC need any updates?"]
    assert a.recordings == 0


# ── a misheard reminder she only chatted about ───────────────────
FRIES = "We might be in five minutes to take my fries out."


@pytest.fixture
def brain():
    async def no(_s):
        return False

    return LocalBrain({"ollama_model": "m", "ollama_url": "http://x"}, {"style": "s", "name": "Mio"},
                      ToolBox(SECURITY, no))


def _script(brain, replies):
    """The model's answers in order; what it was sent is kept."""
    sent = []
    replies = iter(replies)

    async def chat(messages, tools=None, think=False):
        sent.append({"messages": list(messages), "tools": [t["function"]["name"] for t in tools or []]})
        return next(replies)

    brain._chat = chat
    return sent


def _timer_box():
    added = []
    timers = SimpleNamespace(add=lambda _due, label, kind, secs=None: added.append((label, kind, secs)))
    return SimpleNamespace(timers=timers, on_result=None), added


async def test_a_time_with_a_purpose_sets_a_reminder_and_says_so():
    tools, added = _timer_box()
    result = await fastpath._timers(fastpath._clean(FRIES), tools)
    assert added == [("take my fries out", "reminder", 300)]
    assert "fries" in result.reply  # said out loud, so a wrong guess gets noticed and cancelled


@pytest.mark.parametrize("said", [
    # the model set timers for these when it was left to decide
    "I'll be back in five minutes", "the movie starts in an hour", "my pizza was ready in ten minutes yesterday",
    # questions and commands are the model's to read
    "what can I do in five minutes to relax", "can you open steam in five minutes to play",
])
async def test_a_time_alone_sets_nothing(said):
    tools, added = _timer_box()
    assert await fastpath._timers(fastpath._clean(said), tools) is None
    assert added == []


def _row(text, src="local", tools=()):
    return {"t": "2026-11-03T15:07:00", "text": text, "reply": "Sounds good!", "src": src, "secs": 5.2,
            "tools": list(tools)}


@pytest.mark.parametrize("row, flagged", [
    (_row(FRIES), True),
    (_row("wake me in an hour"), True),
    (_row(FRIES, tools=[{"name": "timer", "result": "set"}]), False),
    (_row("What's up?"), False),
])
def test_the_review_flags_a_time_that_nothing_happened_for(row, flagged):
    assert bool(review.problems([row])) is flagged


# ── facts you tell her ────────────────────────────────────────────────────────
@pytest.mark.parametrize("said, want", [
    ("I'm 25.", True),
    ("my friend Alex plays Roblox with me", True),
    ("I really like lofi music", True),
    ("by the way my name is Sam", True),
    ("I'm 25?", False),
    ("Does my PC need any updates?", False),
    ("check my pc for viruses", False),
    ("my screen is flickering", False),
])
def test_what_counts_as_telling_her_about_yourself(said, want):
    assert daemon.tells_about_self(said) is want


def _knows(a, monkeypatch, tmp_path, facts):
    monkeypatch.setattr(memory, "MEMORY_FILE", tmp_path / "memory.json")
    a.memory = memory.Memory(4)
    a._told = set()
    a.asked_model = []

    async def available():
        return True

    async def structured(_system, user, _schema, _n):
        a.asked_model.append(user)
        return {"facts": facts}

    a.local = SimpleNamespace(available=available, _structured=structured)


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_she_knows_it_right_after_you_tell_her(monkeypatch, tmp_path):
    a = _assistant(monkeypatch)
    _knows(a, monkeypatch, tmp_path, ["The user is 25"])
    await _requests(a, ["Oh, cool!"])
    await a._interaction("I'm 25.")
    await _settle()
    assert a.memory.facts == ["The user is 25"]


async def test_a_command_is_not_sent_for_fact_learning_right_away(monkeypatch, tmp_path):
    a = _assistant(monkeypatch)
    _knows(a, monkeypatch, tmp_path, [])
    await _requests(a, ["Done."])
    await a._interaction("check my pc for viruses")
    await _settle()
    assert a.asked_model == []


async def test_the_session_end_does_not_learn_the_same_thing_twice(monkeypatch, tmp_path):
    a = _assistant(monkeypatch)
    _knows(a, monkeypatch, tmp_path, ["The user is 25"])
    del a.end_session  # the real one
    a.tools = SimpleNamespace(forget_outside_text=lambda: None)
    await _requests(a, ["Oh, cool!"])
    await a._interaction("I'm 25.")
    await _settle()
    a.memory.add("I'm 25.", "Oh, cool!")
    a.end_session()
    await _settle()
    assert len(a.asked_model) == 1


# ── small talk ────────────────────────────────────────────────────────────────
CONTEXT = ("Known about the user: Alex is the user's friend\n"
           "Right now — focused window: Prism Launcher\n"
           "These requests of the user worked before like this:\n- open discord")


async def test_small_talk_knows_you_and_what_is_on_screen(brain):
    sent = _script(brain, [{"content": "Still in Prism Launcher?"}])
    await brain._small_talk("What's up?", CONTEXT, None)
    system = sent[0]["messages"][0]["content"]
    assert "Alex is the user's friend" in system
    assert "Prism Launcher" in system


async def test_small_talk_is_told_a_fact_is_not_what_you_are_doing_now(brain):
    """"While you play Roblox with Alex" came from a fact, with Prism Launcher on screen."""
    sent = _script(brain, [{"content": "Hey!"}])
    await brain._small_talk("What's up?", CONTEXT, None)
    assert "only the 'Right now' line says what they are doing" in sent[0]["messages"][0]["content"]


async def test_small_talk_is_not_handed_the_learned_tool_examples(brain):
    sent = _script(brain, [{"content": "Hey!"}])
    await brain._small_talk("What's up?", CONTEXT, None)
    assert "open discord" not in sent[0]["messages"][0]["content"]


async def test_small_talk_is_not_handed_the_date_to_chat_about(brain):
    """"It's a cozy Sunday evening" was her answer to nearly every "what's up?"."""
    sent = _script(brain, [{"content": "Hey!"}])
    await brain._small_talk("What's up?", CONTEXT, None)
    assert clock.now_line() not in sent[0]["messages"][0]["content"]


async def test_what_s_up_reaches_small_talk_with_the_context(brain):
    sent = _script(brain, [{"content": "Hey!"}])
    await brain.respond("What's up?", CONTEXT)
    assert "Prism Launcher" in sent[0]["messages"][0]["content"]
