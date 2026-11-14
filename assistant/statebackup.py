"""A daily copy of everything she has learned (skills, lexicon, facts, timers, reviews…), kept for a week.

Restore one: stop assistantd, then
    tar -xzf ~/.local/share/assistant/backups/state-<date>.tar.gz -C ~/.local/state
and start it again. Archives are private (0600): the state holds tokens and your requests.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import tarfile
from pathlib import Path

from .config import STATE_DIR, XDG_DATA

log = logging.getLogger("backup")

BACKUP_DIR = XDG_DATA / "assistant" / "backups"
KEEP = 7
SKIP_DIRS = {"bench_unlock"}  # benchmark leftovers, not hers


def _skip(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = Path(info.name).parts
    if len(parts) > 1 and parts[1] in SKIP_DIRS:
        return None
    if ".bak-" in parts[-1] or parts[-1].endswith(".tmp"):  # hand-made copies and half-written files
        return None
    return info


def backup(state: Path = STATE_DIR, dest: Path = BACKUP_DIR, keep: int = KEEP, today: dt.date | None = None) -> Path | None:
    """Today's archive (made once per day); older ones beyond `keep` are deleted. None if already done."""
    today = today or dt.date.today()
    out = dest / f"state-{today.isoformat()}.tar.gz"
    if out.exists() or not state.is_dir():
        return None
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    part = out.with_suffix(".part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f, tarfile.open(fileobj=f, mode="w:gz") as tar:
        tar.add(state, arcname=state.name, filter=_skip)
    part.rename(out)  # a crash mid-write never leaves a broken "today" that blocks tomorrow's check
    for old in sorted(dest.glob("state-*.tar.gz"))[:-keep]:
        old.unlink()
    return out


async def run_daily(every_s: float = 3600):
    """In the daemon: check every hour, so a PC that's only on in the evening still gets its copy."""
    while True:
        try:
            if made := await asyncio.to_thread(backup):
                log.info("state backed up to %s (%d KB)", made, made.stat().st_size // 1024)
        except OSError as e:
            log.warning("state backup failed: %s", e)
        await asyncio.sleep(every_s)
