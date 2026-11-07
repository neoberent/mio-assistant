"""The replay buffer records the screen the game is on, even when the game gets there late."""
import asyncio
import json
import re

import pytest

from assistant import clips, gamemode
from assistant.clips import Clipper

SOBER = re.compile(r"^org\.vinegarhq\.Sober$")
SPACES = [{"id": 1, "output": "DP-2", "is_focused": True}, {"id": 2, "output": "HDMI-A-1", "is_focused": False}]


def sober(workspace):
    return {"id": 7, "app_id": "org.vinegarhq.Sober", "workspace_id": workspace, "title": "Sober"}


class FakeRecorder:
    def __init__(self):
        self.returncode = None
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


@pytest.fixture
def clipper(monkeypatch, tmp_path):
    """A Clipper with a scripted niri and a fake recorder; `.screens` lists the monitors it recorded."""
    c = Clipper({"folder": str(tmp_path)})
    c.windows = [sober(1)]
    c.screens = []

    async def niri(*what):
        if c.windows is None:
            raise OSError("niri is gone")
        return c.windows if what == ("windows",) else SPACES

    async def launch(*argv, **_):
        c.screens.append(argv[argv.index("-w") + 1])
        sock = tmp_path / "gsr.sock"
        sock.touch()
        return FakeRecorder()

    monkeypatch.setattr(clips, "_niri", niri)
    monkeypatch.setattr(clips, "SOCK", tmp_path / "gsr.sock")
    monkeypatch.setattr(clips, "SETTLE_S", 0)
    monkeypatch.setattr(clips.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(c, "available", lambda: True)
    return c


def test_it_starts_on_the_game_s_screen(clipper):
    async def go():
        clipper.windows = [sober(2)]
        await clipper.start("Sober", SOBER)
    asyncio.run(go())
    assert clipper.screens == ["HDMI-A-1"]


def test_it_follows_the_game_to_another_screen(clipper):
    async def go():
        await clipper.start("Sober", SOBER)
        clipper.windows = [sober(2)]
        await clipper.game_moved()
    asyncio.run(go())
    assert clipper.screens == ["DP-2", "HDMI-A-1"]
    assert clipper.monitor == "HDMI-A-1"


def test_moving_workspaces_on_the_same_screen_keeps_the_buffer(clipper):
    async def go():
        await clipper.start("Sober", SOBER)
        await clipper.game_moved()
    asyncio.run(go())
    assert clipper.screens == ["DP-2"]


@pytest.mark.parametrize("windows", [None, [], [sober(None)]])
def test_not_knowing_where_the_game_is_keeps_the_buffer(clipper, windows):
    async def go():
        await clipper.start("Sober", SOBER)
        clipper.windows = windows
        await clipper.game_moved()
    asyncio.run(go())
    assert clipper.screens == ["DP-2"]


def test_a_burst_of_moves_restarts_once(clipper):
    async def go():
        await clipper.start("Sober", SOBER)
        clipper.windows = [sober(2)]
        await asyncio.gather(clipper.game_moved(), clipper.game_moved(), clipper.game_moved())
    asyncio.run(go())
    assert clipper.screens == ["DP-2", "HDMI-A-1"]


def test_it_never_saves_a_clip_of_the_wrong_screen(clipper, monkeypatch):
    async def no_save(*_):
        raise AssertionError("saved the wrong screen")
    monkeypatch.setattr(clipper, "_ipc", no_save)

    async def go():
        await clipper.start("Sober", SOBER)
        clipper.windows = [sober(2)]  # moved, and the event never reached us
        return await clipper.save(30)
    path, why = asyncio.run(go())
    assert path is None and "HDMI-A-1" in why
    assert clipper.screens == ["DP-2", "HDMI-A-1"]


class FakeStream:
    """Plays niri events, then ends the run loop by raising."""
    def __init__(self, events):
        self.stdout = self._lines(events)

    async def _lines(self, events):
        for e in events:
            yield (json.dumps(e) + "\n").encode()
            await asyncio.sleep(0)
        raise asyncio.CancelledError


def play(monkeypatch, events):
    moves = []

    async def on_change(_active, _game):
        pass

    async def on_moved():
        moves.append(True)

    async def stream(*_, **__):
        return FakeStream(events)

    async def no_check(self):
        pass

    async def no_set(self, _active):
        pass

    monkeypatch.setattr(gamemode.asyncio, "create_subprocess_exec", stream)
    monkeypatch.setattr(gamemode.GameMode, "_check", no_check)
    monkeypatch.setattr(gamemode.GameMode, "_set", no_set)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(gamemode.GameMode({}, on_change, on_moved).run())
    return len(moves)


def test_game_mode_reports_a_game_changing_workspace(monkeypatch):
    assert play(monkeypatch, [{"WindowsChanged": {"windows": [sober(1)]}},
                              {"WindowOpenedOrChanged": {"window": sober(2)}}]) == 1


def test_game_mode_ignores_the_game_s_title_changing(monkeypatch):
    renamed = {**sober(1), "title": "Roblox"}
    assert play(monkeypatch, [{"WindowsChanged": {"windows": [sober(1)]}},
                              {"WindowOpenedOrChanged": {"window": renamed}}]) == 0


def test_game_mode_ignores_other_windows(monkeypatch):
    kitty = {"id": 3, "app_id": "kitty", "workspace_id": 2, "title": "⠋ working"}
    assert play(monkeypatch, [{"WindowsChanged": {"windows": [sober(1)]}},
                              {"WindowOpenedOrChanged": {"window": kitty}}]) == 0


def test_game_mode_reports_a_workspace_moving_screens(monkeypatch):
    assert play(monkeypatch, [{"WindowsChanged": {"windows": [sober(1)]}},
                              {"WorkspacesChanged": {"workspaces": SPACES}}]) == 1


def test_workspace_changes_without_a_game_mean_nothing(monkeypatch):
    assert play(monkeypatch, [{"WindowsChanged": {"windows": []}},
                              {"WorkspacesChanged": {"workspaces": SPACES}}]) == 0
