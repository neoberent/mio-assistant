"""Crash-safe file writes for state files (memory, usage counter, timers, skills …)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path | str, text: str, encoding: str = "utf-8") -> None:
    """Write `text` to `path` so the file is always either the old or the new version.

    Path.write_text truncates first, so a kill, power loss or full disk mid-write leaves an
    empty/partial file. Here the data goes to a temp file in the same directory, is fsynced,
    then os.replace()d over the target (atomic on POSIX); the directory is fsynced so the
    rename itself survives a power cut. On any error the temp file is removed and the old
    file is left untouched.
    """
    path = Path(path)
    try:
        mode = path.stat().st_mode & 0o7777
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    try:
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)
