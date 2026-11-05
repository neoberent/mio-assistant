"""Talks to Mio's browser extension through the native messaging host's socket (see browser_host.py)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
from pathlib import Path

from .config import RUNTIME

log = logging.getLogger("browser")

SOCK = RUNTIME / "mio-browser.sock"
NOT_CONNECTED = ("the browser extension isn't connected. Zen has to be open with the Mio extension installed "
                 "(scripts/install_browser.sh shows how)")


async def call(action: str, **args) -> dict:
    """{"result": …} or {"error": "…"}."""
    try:
        r, w = await asyncio.wait_for(asyncio.open_unix_connection(str(SOCK), limit=16 << 20), 2)  # big pages: >64 KiB
    except (OSError, asyncio.TimeoutError):
        return {"error": NOT_CONNECTED}
    try:
        w.write((json.dumps({"action": action, "args": args}) + "\n").encode())
        await w.drain()
        line = await asyncio.wait_for(r.readline(), 25)
        return json.loads(line) if line else {"error": NOT_CONNECTED}
    except (OSError, asyncio.TimeoutError, ValueError):  # ValueError: bad JSON or an over-long line
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


async def audio_clip(url: str, start: float = 0, seconds: int = 240) -> "np.ndarray | None":
    """`seconds` of the video's audio from `start`, as 16 kHz mono float32 (for Whisper). yt-dlp fetches the
    smallest audio-only file (a whole hour is ~25 MB, ~3 s; its --download-sections streams at only ~2x real
    time, and a bare ffmpeg on the stream URL gets 403), then ffmpeg cuts the part. None on failure."""
    import numpy as np

    a = max(0, int(start))
    with tempfile.TemporaryDirectory(dir=Path.home() / ".cache") as tmp:  # not /tmp: it's RAM here
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "-f", "worstaudio[acodec!=none]/bestaudio/best", "--no-playlist", "--max-filesize", "200M",
            "-o", f"{tmp}/a.%(ext)s", url, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), 90)
        except asyncio.TimeoutError:
            proc.kill()
            return None
        files = [f for f in Path(tmp).glob("a.*") if not f.name.endswith(".part")]
        if not files:
            log.info("audio clip failed: %s", err.decode(errors="replace")[-300:])
            return None
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "error", "-ss", str(a), "-t", str(seconds), "-i", str(files[0]), "-vn", "-ac", "1", "-ar", "16000",
            "-f", "f32le", "-", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        raw, _ = await proc.communicate()
    audio = np.frombuffer(raw, dtype=np.float32)
    return audio if audio.size > 16000 * 3 else None
