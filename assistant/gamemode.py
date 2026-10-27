"""Game mode: while a game window is open, Mio gives her GPU memory back.

Watches niri's event stream (no polling). On entering: Whisper and the Ollama model are
unloaded, the wake word pauses, and the avatar stays hidden (voice only). Push-to-talk still
works — models reload on demand and are unloaded again shortly after.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

log = logging.getLogger("gamemode")

DEFAULT_GAMES = r"^(steam_app_\d+|org\.vinegarhq\.Sober|gamescope|.*\.exe|minecraft.*|com\.mojang.*|" \
                r"org\.prismlauncher.*|osu!?|.*lutris.*)$"


class GameMode:
    def __init__(self, cfg: dict, on_change):
        self.pattern = re.compile(cfg.get("game_app_ids", DEFAULT_GAMES), re.I)
        self.on_change = on_change  # async callable(active: bool, game: str)
        self.active = False
        self.game = ""

    async def _game_window(self) -> str:
        proc = await asyncio.create_subprocess_exec("niri", "msg", "-j", "windows",
                                                    stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        try:
            for w in json.loads(out):
                if self.pattern.match(w.get("app_id") or ""):
                    return w.get("title") or w.get("app_id")
        except json.JSONDecodeError:
            pass
        return ""

    async def _check(self):
        game = await self._game_window()
        if bool(game) != self.active:
            self.active, self.game = bool(game), game
            log.info("game mode %s%s", "on" if game else "off", f" ({game})" if game else "")
            await self.on_change(self.active, game)

    async def run(self):
        await self._check()
        while True:
            proc = await asyncio.create_subprocess_exec("niri", "msg", "-j", "event-stream",
                                                        stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.DEVNULL, limit=1 << 22)
            games: set[int] = set()
            async for line in proc.stdout:
                if not line.startswith(b'{"Window'):
                    continue  # cheap pre-filter; animated titles fire these constantly
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                changed = False
                if "WindowsChanged" in ev:
                    now = {w["id"] for w in ev["WindowsChanged"]["windows"] if self.pattern.match(w.get("app_id") or "")}
                    changed, games = now != games, now
                elif "WindowOpenedOrChanged" in ev:
                    w = ev["WindowOpenedOrChanged"]["window"]
                    if self.pattern.match(w.get("app_id") or "") and w["id"] not in games:
                        games.add(w["id"])
                        changed = True
                elif "WindowClosed" in ev and ev["WindowClosed"]["id"] in games:
                    games.discard(ev["WindowClosed"]["id"])
                    changed = True
                if changed:
                    await self._set(bool(games))
            await proc.wait()
            await asyncio.sleep(5)  # niri restarted? reconnect

    async def _set(self, active: bool):
        if active == self.active:
            return
        if not active:
            await asyncio.sleep(3)  # games often close one window and open another while loading
            if await self._game_window():
                return
        await self._check()
