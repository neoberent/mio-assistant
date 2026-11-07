"""Instant replay ("clip that"): gpu-screen-recorder keeps the last few minutes in RAM while a game runs.

The recorder starts with game mode (or always, if configured) on the monitor the game is on, encodes on
the GPU's NVENC chip (barely any FPS cost), and saves on request through its IPC socket. Clips are
renamed after the game and land in [clips] folder (default ~/Videos/Clips).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

from .config import PROJECT_DIR, RUNTIME

log = logging.getLogger("clips")

SOCK = RUNTIME / "mio-gsr.sock"
MAX_LAG = 20  # seconds between "clip that" being said and the save, added so the moment isn't cut off
WINDOW = PROJECT_DIR / "bin" / "assistant-clip-window"
SETTLE_S = 1.0  # wait for a moved game to land, so a burst of niri events restarts the recorder once


async def _niri(*what) -> list | dict:
    p = await asyncio.create_subprocess_exec("niri", "msg", "-j", *what, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.DEVNULL)
    try:
        return json.loads((await asyncio.wait_for(p.communicate(), 5))[0] or b"[]")
    except asyncio.TimeoutError:
        p.kill()
        raise


def latest_clip(folder: Path) -> Path | None:
    """The newest clip by time, so it also works after a restart and for clips saved by hotkey."""
    try:
        return max(folder.glob("*.mp4"), key=lambda p: p.stat().st_mtime, default=None)
    except OSError:
        return None


class Clipper:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.folder = Path(os.path.expanduser(cfg.get("folder", "~/Videos/Clips")))
        self.buffer_s = int(cfg.get("buffer_s", 180))
        self.default_s = int(cfg.get("default_s", 30))
        self.proc: asyncio.subprocess.Process | None = None
        self.game = ""
        self.pattern: re.Pattern | None = None
        self.monitor = ""
        self._moves = 0
        self.started = 0.0
        self.asked_at = 0.0  # when the user started speaking; set by the daemon
        self._lock = asyncio.Lock()
        self.window: asyncio.subprocess.Process | None = None

    def available(self) -> bool:
        return shutil.which("gpu-screen-recorder") is not None

    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def _game_output(self, pattern: re.Pattern | None) -> str | None:
        """The monitor the game window is on, or None if that can't be told right now."""
        try:
            wins, spaces = await _niri("windows"), await _niri("workspaces")
        except (json.JSONDecodeError, asyncio.TimeoutError, OSError):
            return None
        outputs = {w["id"]: w["output"] for w in spaces}
        game = next((w for w in wins if pattern and pattern.match(w.get("app_id") or "")), None)
        return outputs.get(game.get("workspace_id")) if game else None

    async def _monitor_of(self, pattern: re.Pattern | None) -> str:
        """Output of the game window (else the focused one)."""
        if output := await self._game_output(pattern):
            return output
        try:
            spaces = await _niri("workspaces")
        except (json.JSONDecodeError, asyncio.TimeoutError, OSError):
            return "screen"
        return next((w["output"] for w in spaces if w.get("is_focused")), None) or "screen"

    async def start(self, game: str = "", pattern: re.Pattern | None = None):
        async with self._lock:
            if self.running() or not self.available():
                return
            self.game, self.pattern = game, pattern
            await self._launch(await self._monitor_of(pattern))

    async def _launch(self, monitor: str):
        self.monitor = monitor
        self.folder.mkdir(parents=True, exist_ok=True)
        audio = "|".join(["default_output"] + (["default_input"] if self.cfg.get("mic", False) else []))
        argv = ["gpu-screen-recorder", "-w", self.monitor, "-c", "mp4", "-f", str(self.cfg.get("fps", 60)),
                "-k", self.cfg.get("codec", "h264"), "-bm", "cbr", "-q", str(self.cfg.get("bitrate_kbps", 50000)),
                "-tune", "quality", "-a", audio, "-ac", "aac", "-ab", "256", "-r", str(self.buffer_s), "-replay-storage", "ram",
                "-cursor", "no", "-o", str(self.folder), "-ipc", str(SOCK)]
        SOCK.unlink(missing_ok=True)  # a leftover socket would pass for "capture is up"
        self.proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.DEVNULL,
                                                         stderr=asyncio.subprocess.PIPE)
        self.started = time.monotonic()
        for _ in range(50):  # the socket appears once capture is up
            if SOCK.exists() or self.proc.returncode is not None:
                break
            await asyncio.sleep(0.1)
        if self.proc.returncode is not None:
            err = (await self.proc.stderr.read()).decode(errors="replace").strip().splitlines()[-3:]
            log.warning("replay buffer failed to start: %s", " | ".join(err))
            self.proc = None
            return
        asyncio.create_task(self._drain(self.proc))
        log.info("replay buffer on %s (%s s, %s)", self.monitor, self.buffer_s, self.game or "no game")

    async def _follow(self) -> str | None:
        """Moves the recorder to the game's monitor if it isn't there. Returns the new monitor if it moved.
        A recorder can only watch one monitor, so moving it starts a new buffer."""
        async with self._lock:
            if not self.running() or self.pattern is None:
                return None
            now = await self._game_output(self.pattern)
            if not now or now == self.monitor:
                return None
            log.info("the game moved from %s to %s", self.monitor, now)
            await self._halt()
            await self._launch(now)
            return now

    async def game_moved(self):
        """Niri says the game window or a workspace changed place."""
        self._moves += 1
        mine = self._moves
        await asyncio.sleep(SETTLE_S)
        if mine == self._moves:
            await self._follow()

    async def _drain(self, proc):
        """Keep stderr from filling up its pipe; log real errors."""
        async for line in proc.stderr:
            text = line.decode(errors="replace").strip()
            if re.search(r"\b(error|failed)\b", text, re.I):
                log.warning("gsr: %s", text[:200])

    async def stop(self):
        async with self._lock:
            await self._halt()

    async def _halt(self):
        if not self.running():
            self.proc = None
            return
        self.proc.terminate()  # SIGTERM/SIGINT in replay mode = stop without saving
        try:
            await asyncio.wait_for(self.proc.wait(), 5)
        except asyncio.TimeoutError:
            self.proc.kill()
        self.proc = None
        log.info("replay buffer off")

    async def _ipc(self, name: str, data=None, timeout: float = 30) -> dict:
        r, w = await asyncio.wait_for(asyncio.open_unix_connection(str(SOCK)), 3)
        try:
            w.write((json.dumps({"id": 1, "name": name, "data": data}) + "\n").encode())
            await w.drain()
            return json.loads(await asyncio.wait_for(r.readline(), timeout))
        finally:
            w.close()

    async def save(self, seconds: int | None = None) -> tuple[Path | None, str]:
        """Saves the last `seconds` (plus the time it took to ask). Returns (path, message)."""
        if not self.running():
            return None, "not running"
        if moved := await self._follow():  # safety net: a clip of the wrong screen is worse than none
            return None, f"I was recording the wrong screen, so I moved to {moved}. Anything from now on can be clipped"
        recorded = time.monotonic() - self.started
        lag = time.monotonic() - self.asked_at
        lag = lag if self.asked_at and lag <= MAX_LAG else 0  # an older push-to-talk wasn't about this clip
        self.asked_at = 0.0
        secs = int(min((seconds or self.default_s) + lag, self.buffer_s, recorded))
        resp = await self._ipc("save-replay", {"seconds": max(2, secs)})
        if resp.get("result") != "ok" or not resp.get("data"):
            return None, str(resp.get("data") or "save failed")
        src = Path(resp["data"])
        game = "Roblox" if re.search(r"sober|roblox", self.game, re.I) else self.game
        name = re.sub(r"[^\w .()-]+", "", game).strip()[:50] or "Clip"
        dst = self.folder / f"{name} {dt.datetime.now().strftime('%Y-%m-%d %H-%M-%S')}.mp4"
        try:
            src.rename(dst)
        except OSError:
            dst = src
        log.info("clip saved: %s (%d s)", dst, secs)
        return dst, f"{secs} seconds"

    async def open_upload_window(self) -> str:
        """Opens the window for the newest clip. Nothing is uploaded until the user presses Upload there."""
        if self.window is not None and self.window.returncode is None:
            return "the upload window is already open"
        clip = latest_clip(self.folder)
        if clip is None:
            return f"there are no clips in {self.folder} yet"
        env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
        self.window = await asyncio.create_subprocess_exec(str(WINDOW), str(clip), env=env, stdout=asyncio.subprocess.DEVNULL,
                                                           stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        await asyncio.sleep(0.5)  # a window that can't start ends right away: say so instead of "it's open"
        if self.window.returncode not in (None, 0):
            return "the upload window couldn't open"
        log.info("upload window for %s", clip.name)
        return f"opened the upload window for {clip.name}"
