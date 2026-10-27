"""Capture the focused window (niri) and read it: OCR locally, or hand a small image to Claude."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .config import RUNTIME

SHOT = RUNTIME / "mio-window.png"
OCR_IN = RUNTIME / "mio-window-ocr.png"
VISION = RUNTIME / "mio-window.jpg"


async def _run(*argv: str, input_: bytes | None = None, timeout: float = 20) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL,
                                                stdin=asyncio.subprocess.PIPE if input_ is not None else
                                                asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(input_), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, b""
    return proc.returncode or 0, out


async def _save_clipboard() -> tuple[str, bytes] | None:
    code, types = await _run("wl-paste", "--list-types")
    if code:
        return None
    types_l = types.decode().split()
    for t in ("text/plain;charset=utf-8", "UTF8_STRING", "text/plain", "image/png"):
        if t in types_l:
            code, data = await _run("wl-paste", "--no-newline", "--type", t)
            return (t, data) if code == 0 else None
    return None


async def _restore_clipboard(saved: tuple[str, bytes] | None):
    if saved:
        await _run("wl-copy", "--type", saved[0], input_=saved[1])
    else:
        await _run("wl-copy", "--clear")


async def focused_window() -> dict:
    code, out = await _run("niri", "msg", "-j", "focused-window")
    try:
        return json.loads(out) or {}
    except json.JSONDecodeError:
        return {}


async def capture() -> tuple[Path | None, dict]:
    """Screenshot of the focused window only. niri also copies it to the clipboard, so the
    previous clipboard content is put back afterwards."""
    win = await focused_window()
    saved = await _save_clipboard()
    SHOT.unlink(missing_ok=True)
    await _run("niri", "msg", "action", "screenshot-window", "--write-to-disk", "true", "--path", str(SHOT))
    for _ in range(30):
        if SHOT.exists() and SHOT.stat().st_size > 0:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.3)  # let niri finish setting the clipboard before restoring it
    await _restore_clipboard(saved)
    return (SHOT if SHOT.exists() else None), win


async def ocr(path: Path) -> str:
    # upscaling + grayscale makes tesseract much better at small UI fonts
    await _run("magick", str(path), "-colorspace", "Gray", "-resize", "200%", str(OCR_IN))
    code, out = await _run("tesseract", str(OCR_IN), "-", "--psm", "3", "-l", "eng", timeout=40)
    OCR_IN.unlink(missing_ok=True)
    lines = [l.strip() for l in out.decode(errors="replace").splitlines()]
    return "\n".join(l for l in lines if len(l) > 1)


async def for_vision(path: Path) -> Path:
    """~1280 px JPEG: plenty for reading UI, about 1–1.5k image tokens."""
    await _run("magick", str(path), "-resize", "1280x1280>", "-quality", "82", str(VISION))
    return VISION
