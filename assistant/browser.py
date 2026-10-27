"""Talks to Mio's browser extension through the native messaging host's socket (see browser_host.py)."""
from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path

from .config import RUNTIME

SOCK = RUNTIME / "mio-browser.sock"
NOT_CONNECTED = ("the browser extension isn't connected. Brave has to be open with the Mio extension installed "
                 "(scripts/install_browser.sh, then load browser/extension in brave://extensions)")


async def call(action: str, **args) -> dict:
    """{"result": …} or {"error": "…"}."""
    try:
        r, w = await asyncio.wait_for(asyncio.open_unix_connection(str(SOCK)), 2)
    except (OSError, asyncio.TimeoutError):
        return {"error": NOT_CONNECTED}
    try:
        w.write((json.dumps({"action": action, "args": args}) + "\n").encode())
        await w.drain()
        line = await asyncio.wait_for(r.readline(), 25)
        return json.loads(line) if line else {"error": NOT_CONNECTED}
    except (OSError, asyncio.TimeoutError, json.JSONDecodeError):
        return {"error": "the browser didn't answer"}
    finally:
        w.close()


def _vtt_text(vtt: str) -> str:
    """Subtitle file → plain text; auto-captions repeat every line two or three times."""
    out: list[str] = []
    for line in vtt.splitlines():
        line = re.sub(r"<[^>]+>", "", line).strip()
        if not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")) or line.isdigit():
            continue
        if not out or line != out[-1]:
            out.append(line)
    return " ".join(out)


async def transcript(url: str, limit: int = 6000) -> str:
    """The video's subtitles (manual first, else auto-generated) via yt-dlp. Free and local."""
    with tempfile.TemporaryDirectory() as tmp:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,en,de.*,de",
            "--sub-format", "vtt", "--no-playlist", "-o", f"{tmp}/v.%(ext)s", url,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            await asyncio.wait_for(proc.communicate(), 60)
        except asyncio.TimeoutError:
            proc.kill()
            return ""
        files = sorted(Path(tmp).glob("*.vtt"), key=lambda p: (".en" not in p.name, "orig" in p.name, p.name))
        if not files:
            return ""
        text = _vtt_text(files[0].read_text(errors="replace"))
    return text[:limit] + (" …" if len(text) > limit else "")
