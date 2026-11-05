"""The brain shouldn't load twice, think when it needn't, or sit on the GPU during games."""
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from assistant import tts as tts_mod
from assistant.brain.local import LocalBrain
from tests.test_daemon_logic import _assistant
from tests.test_shell_policy import SECURITY
from assistant.tools import ToolBox


async def _no(_s):
    return False


@pytest.fixture
def brain():
    return LocalBrain({"ollama_model": "m", "ollama_url": "http://x"}, {"style": "s", "name": "Mio"}, ToolBox(SECURITY, _no))


class _Client:
    """Records what would go to Ollama."""

    def __init__(self):
        self.bodies = []

    async def post(self, _path, json=None, **_k):  # noqa: A002
        self.bodies.append(json)
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"message": {"content": '{"steps": ["a"], "op": "other"}'}})

    async def aclose(self):
        pass


async def test_every_call_uses_the_same_context_so_ollama_never_reloads_the_model(brain):
    """Ollama restarts a loaded model when num_ctx differs: a preload at another size is wasted."""
    brain.client = client = _Client()
    await brain.preload()
    await brain._chat([{"role": "user", "content": "x"}])
    await brain._structured("s", "u", {"type": "object"})
    await brain.plan("open discord and then start roblox")
    await brain.browser_step("click the first video")
    sizes = {b["options"]["num_ctx"] for b in client.bodies}
    assert len(client.bodies) == 5
    assert len(sizes) == 1


def _thinks(brain, monkeypatch, fail_first=False):
    seen = []

    async def chat(_messages, _specs=None, think=False):
        seen.append(think)
        if fail_first and len(seen) == 1:
            return {"content": ""}  # an empty reply: she has to try again
        return {"content": "Sure, here you go."}

    monkeypatch.setattr(brain, "_chat", chat)
    return seen


@pytest.mark.parametrize("request_, first", [("what's my disk usage", False), ("why is my disk so full", True)])
async def test_lazy_mode_thinks_up_front_only_about_hard_questions(brain, monkeypatch, request_, first):
    brain.cfg["local_thinking"] = "lazy"
    seen = _thinks(brain, monkeypatch)
    await brain.respond(request_, "")
    assert seen[0] is first


async def test_auto_mode_still_thinks_before_acting(brain, monkeypatch):
    brain.cfg["local_thinking"] = "auto"
    seen = _thinks(brain, monkeypatch)
    await brain.respond("what's my disk usage", "")
    assert seen[0] is True


class _K:
    def __init__(self, *_a):
        pass

    def create(self, *_a, **_k):
        return np.zeros(1, np.float32), 11111


@pytest.fixture
def voice(monkeypatch):
    state = SimpleNamespace(starts=0, kills=0)

    class Worker:
        def __init__(self, _cfg):
            self.lock = __import__("threading").Lock()

        def start(self):
            state.starts += 1

        def synth(self, _t):
            return np.zeros(1, np.float32), 24000

        def close(self):
            state.kills += 1

    monkeypatch.setattr(tts_mod, "_Chatterbox", Worker)
    monkeypatch.setitem(sys.modules, "kokoro_onnx", SimpleNamespace(Kokoro=_K))
    monkeypatch.setattr(tts_mod, "_notify", lambda _t: None)
    return state


def test_during_a_game_the_real_voice_is_gone_and_she_still_speaks(voice):
    t = tts_mod.TTS({"engine": "chatterbox"})
    t.load()
    t.step_aside()
    assert voice.kills == 1
    assert t._synth("hi")[1] == 11111
    assert voice.starts == 1  # speaking doesn't bring the heavy voice back mid-game


def test_after_the_game_her_voice_returns_and_the_backup_leaves_memory(voice):
    t = tts_mod.TTS({"engine": "chatterbox"})
    t.load()
    t.step_aside()
    t._synth("hi")
    t.come_back()
    assert voice.starts == 2
    assert t._kokoro is None
    assert t._synth("hi")[1] == 24000


def test_a_voice_that_never_used_chatterbox_ignores_all_this(voice):
    t = tts_mod.TTS({"engine": "kokoro"})
    t.step_aside()
    t.come_back()
    assert voice.starts == 0
    assert t._synth("hi")[1] == 11111


def test_a_game_opening_while_her_voice_is_still_starting_keeps_it_unloaded(voice, monkeypatch):
    t = tts_mod.TTS({"engine": "chatterbox"})
    real_start = tts_mod._Chatterbox.start

    def start(self):
        real_start(self)
        t.step_aside()  # the game window appears during the ~10 s start

    monkeypatch.setattr(tts_mod._Chatterbox, "start", start)
    t.load()
    assert t._chatterbox is None
    assert voice.kills == 1


def _gaming(monkeypatch, free_voice=True):
    a = _assistant(monkeypatch)
    calls = []
    a.cfg.gamemode, a.cfg.clips = {"free_voice": free_voice}, {"enabled": False}
    a.tts = SimpleNamespace(step_aside=lambda: calls.append("aside"), come_back=lambda: calls.append("back"))
    a.stt = SimpleNamespace(_model=object(), load=lambda: None, unload=lambda: None)
    a.tools = SimpleNamespace(gaming=False)
    a.wake, a._regame_task, a.gamemode = None, None, None

    async def nothing():
        return None

    a.local = SimpleNamespace(unload=nothing)
    a.avatar = SimpleNamespace(send=lambda **_k: nothing())
    return a, calls


async def test_a_game_frees_her_voice_and_closing_it_brings_it_back(monkeypatch):
    a, calls = _gaming(monkeypatch)
    await a._on_gamemode(True, "Roblox")
    assert calls == ["aside"]
    await a._on_gamemode(False, "")
    assert calls == ["aside", "back"]


async def test_the_user_can_keep_her_voice_during_games(monkeypatch):
    a, calls = _gaming(monkeypatch, free_voice=False)
    await a._on_gamemode(True, "Roblox")
    await a._on_gamemode(False, "")
    assert calls == []


def _lazy(brain, monkeypatch, *answers):
    """The model answers in turn; returns the think flag of each call."""
    brain.cfg["local_thinking"] = "lazy"
    seen, replies = [], iter(answers)

    async def chat(_messages, _specs=None, think=False):
        seen.append(think)
        return next(replies)

    async def tool(**_k):
        return "12 GB free"

    brain.tools.tools["system_info"].fn = tool
    monkeypatch.setattr(brain, "_chat", chat)
    return seen


WORDS = {"content": "Sure, it looks fine to me."}
CALL = {"content": "", "tool_calls": [{"function": {"name": "system_info", "arguments": {}}}]}


async def test_lazy_mode_skips_thinking_when_she_just_acts(brain, monkeypatch):
    seen = _lazy(brain, monkeypatch, CALL, WORDS)
    await brain.respond("check my system info", "")
    assert seen == [False, False]


async def test_lazy_mode_thinks_when_she_answers_a_request_without_acting(brain, monkeypatch):
    seen = _lazy(brain, monkeypatch, WORDS, CALL, WORDS)
    await brain.respond("check my system info", "")
    assert seen[:2] == [False, True]


async def test_lazy_mode_rethinks_only_once(brain, monkeypatch):
    seen = _lazy(brain, monkeypatch, WORDS, WORDS, WORDS, WORDS)
    assert await brain.respond("check my system info", "")
    assert seen.count(True) == 1


async def test_lazy_mode_leaves_small_talk_alone(brain, monkeypatch):
    seen = _lazy(brain, monkeypatch, WORDS, WORDS)
    await brain.respond("thanks, you're so cute", "")
    assert True not in seen
