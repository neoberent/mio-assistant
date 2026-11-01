"""Background-task helpers: nothing fire-and-forget gets lost or dies silently."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

log = logging.getLogger("aio")

# asyncio only keeps weak references to tasks: an unreferenced task can be garbage-collected mid-run.
_tasks: set[asyncio.Task] = set()


def _done(task: asyncio.Task):
    _tasks.discard(task)
    if task.cancelled():
        return
    if (e := task.exception()) is not None:
        log.error("background task %s crashed", task.get_name(), exc_info=e)


def spawn(coro: Awaitable, name: str | None = None) -> asyncio.Task:
    """create_task that keeps the task alive until it finishes and logs it if it crashes."""
    task = asyncio.ensure_future(coro)
    if name:
        task.set_name(name)
    _tasks.add(task)
    task.add_done_callback(_done)
    return task


def supervise(factory: Callable[[], Awaitable], name: str, backoff: float = 5, max_backoff: float = 300,
              healthy_after: float = 600) -> asyncio.Task:
    """Run a long-lived loop (timers, game mode, …); restart it if it crashes instead of losing the feature.

    Returning normally means "done on purpose" (e.g. the feature is disabled) and is not restarted. The wait
    between restarts doubles up to `max_backoff`, and resets once a run has lasted `healthy_after` seconds.
    """
    async def runner():
        delay = backoff
        while True:
            started = asyncio.get_running_loop().time()
            try:
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s crashed; restarting in %.0fs", name, delay)
            if asyncio.get_running_loop().time() - started >= healthy_after:
                delay = backoff
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_backoff)
    return spawn(runner(), name)
