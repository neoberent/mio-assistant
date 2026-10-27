"""What's going on right now, in one short line for her brain: the focused window, the browser tab, the open
Discord chat, what's playing, a running game and timers. "What game is this?", "skip this", "tell them …" stop
being guesses. Two quick local commands (~15 ms), no screenshots, nothing leaves the PC except as part of a
request that goes to Claude anyway.
"""
from __future__ import annotations

import asyncio
import json
import re

BROWSERS = re.compile(r"brave|chrom|firefox", re.I)


async def _run(*argv: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 2)
        return out.decode(errors="replace").strip() if proc.returncode == 0 else ""
    except (OSError, asyncio.TimeoutError):
        return ""


def _short(title: str, n: int = 70) -> str:
    title = re.sub(r"\s+-\s+(Brave( Origin)?|Chromium|Mozilla Firefox|Discord)$", "", title or "").strip()
    return title if len(title) <= n else title[:n - 1] + "…"


async def snapshot(game: str = "", timers=None) -> tuple[str, list[str]]:
    """→ (the 'Right now' line, names of the people/channel in the open Discord chat)."""
    windows_raw, playing = await asyncio.gather(
        _run("niri", "msg", "-j", "windows"),
        _run("playerctl", "metadata", "--format", "{{status}}|{{artist}}|{{title}}"))
    try:
        windows = json.loads(windows_raw) if windows_raw else []
    except json.JSONDecodeError:
        windows = []
    parts: list[str] = []
    focused = next((w for w in windows if w.get("is_focused")), None)
    if focused:
        parts.append(f"focused window: {focused.get('app_id')} — {_short(focused.get('title'))}")
    browser = next((w for w in windows if BROWSERS.search(w.get("app_id") or "")), None)
    if browser and browser is not focused:
        parts.append(f"browser tab: {_short(browser.get('title'))}")
    names: list[str] = []
    discord = next((w for w in windows if (w.get("app_id") or "").lower() == "discord"), None)
    if discord and (chat := _short(discord.get("title"))) and chat.lower() not in ("discord", "friends"):
        parts.append(f"Discord chat open: {chat}")
        if not chat.startswith("#"):  # a DM or group DM: the people in it
            names = [n.strip().lstrip("@") for n in chat.split(",") if re.search(r"[A-Za-z]{3}", n)]
    if playing:
        status, artist, title = (playing.split("|") + ["", ""])[:3]
        what = " — ".join(x for x in (title.strip(), artist.strip()) if x)
        if what and _short(what) not in " ".join(parts):  # a paused browser tab is already listed
            parts.append(f"media {status.lower()}: {_short(what)}")
    if game:
        parts.append(f"game running: {_short(game)}")
    if timers is not None and getattr(timers, "items", None):
        parts.append(f"timers: {timers.summary()}")
    return ("Right now — " + "; ".join(parts) + ".") if parts else "", names
