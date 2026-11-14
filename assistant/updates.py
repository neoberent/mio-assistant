"""After "install the updates": follow the update terminal and say how it went, and whether to restart."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
import shlex
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("updates")

PACMAN_LOG = Path("/var/log/pacman.log")
MODULES = Path("/usr/lib/modules")
# needs a restart to take effect (pipewire/wireplumber would do with a re-login, but a restart is the simple advice)
RESTART = re.compile(r"^(linux\S*|nvidia\S*|systemd|glibc|mesa|pipewire|wireplumber|niri)$")
LINE = re.compile(r"^\[([^\]]+)\] \[ALPM\] (upgraded|installed|removed) (\S+) \(([^)]*)\)")


def status_file() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"mio-update-{os.getpid()}.status"


def terminal_script(command: str, status: Path) -> str:
    """The update runs as usual; its exit code is written down before the window waits for Enter."""
    return (f"rm -f {shlex.quote(str(status))}; {command}; echo $? > {shlex.quote(str(status))}; echo; "
            "read -r -p 'Done. Press Enter to close this window.' _")


def changes_since(log_text: str, since: dt.datetime) -> list[tuple[str, str]]:
    """pacman.log → [(action, package)] for everything after `since` (timezone aware)."""
    out = []
    for line in log_text.splitlines():
        m = LINE.match(line)
        if not m:
            continue
        try:
            when = dt.datetime.fromisoformat(m[1])
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.astimezone()
        if when >= since:
            out.append((m[2], m[3]))
    return out


def kernel_replaced(running: str | None = None, modules: Path = MODULES) -> bool:
    """The running kernel's modules are gone: a kernel update happened and only a restart loads the new one."""
    running = running or os.uname().release
    return modules.is_dir() and not (modules / running).is_dir()


def summary(code: int, changes: list[tuple[str, str]], kernel_gone: bool) -> str:
    upgraded = [p for a, p in changes if a in ("upgraded", "installed")]
    n = len(upgraded)
    if not n:
        if code == 0:
            return "The update finished, there was nothing to install."
        return "The update didn't go through, so nothing was installed."
    what = f"{n} package{'s' if n != 1 else ''}"
    text = f"The update is done, {what} updated." if code == 0 else \
        f"The update stopped with an error after updating {what}. Have a look at the terminal."
    restart = sorted({p for p in upgraded if RESTART.match(p)})
    if kernel_gone or restart:
        names = [p for p in restart if not p.startswith("linux")]
        why = "the kernel" if kernel_gone or any(p.startswith("linux") for p in restart) else ""
        parts = [x for x in [why] + names[:3] if x]
        said = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        text += f" Restart your PC when you get a chance, {said} {'was' if len(parts) == 1 else 'were'} updated."
    return text


async def watch(status: Path, since: dt.datetime, on_done: Callable[[str], Awaitable[None]],
                timeout_s: float = 4 * 3600, every_s: float = 3):
    """Wait for the terminal's exit code, then report. Closing the window early (no code) stays silent."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    while loop.time() < start + timeout_s:
        await asyncio.sleep(every_s)
        if status.exists():
            break
        if loop.time() - start > 15 and not await _terminal_open(status):  # give the window time to appear
            log.info("update terminal closed without finishing")
            return
    else:
        return
    try:
        code = int(status.read_text().strip() or 1)
    except (OSError, ValueError):
        code = 1
    status.unlink(missing_ok=True)
    try:
        text = await asyncio.to_thread(PACMAN_LOG.read_text, errors="replace")
    except OSError:
        text = ""
    report = summary(code, changes_since(text, since), kernel_replaced())
    log.info("update done (exit %s): %s", code, report)
    await on_done(report)


async def _terminal_open(status: Path) -> bool:
    """Is the bash that writes this status file still running?"""
    proc = await asyncio.create_subprocess_exec("pgrep", "-f", f"[e]cho \\$\\? > {status}",
                                                stdout=asyncio.subprocess.DEVNULL)
    return await proc.wait() == 0
