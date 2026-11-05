"""One `LISTEN live_preview` connection per API process, fanned out to WebSocket subscribers.

Payloads are our job ids only (the worker never puts frames in NOTIFY). A notification just
wakes the subscribers of that job; they read the row themselves. Subscribers also poll, so a
lost listener connection only adds latency (it reconnects in the background).
"""

import asyncio
import logging
from collections import defaultdict

import psycopg

from app.core.config import get_settings

logger = logging.getLogger(__name__)
CHANNEL = "live_preview"
RETRY_SECONDS = 2.0


class LiveNotifier:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self._subscribers: defaultdict[str, set[asyncio.Event]] = defaultdict(set)
        self._task: asyncio.Task | None = None

    def subscribe(self, job_id: str, wake: asyncio.Event) -> None:
        self._subscribers[job_id].add(wake)
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._listen())

    def unsubscribe(self, job_id: str, wake: asyncio.Event) -> None:
        waiting = self._subscribers.get(job_id)
        if waiting is not None:
            waiting.discard(wake)
            if not waiting:
                del self._subscribers[job_id]

    def _dispatch(self, job_id: str) -> None:
        for wake in self._subscribers.get(job_id, ()):
            wake.set()

    async def _listen(self) -> None:
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self.database_url, autocommit=True
                ) as conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    async for note in conn.notifies():
                        self._dispatch(note.payload)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("LIVE_NOTIFY_UNAVAILABLE: %s", type(error).__name__)
            await asyncio.sleep(RETRY_SECONDS)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


_notifiers: dict[int, LiveNotifier] = {}


def notifier() -> LiveNotifier:
    """The listener of the running event loop (tests and servers may run several loops)."""
    loop = id(asyncio.get_running_loop())
    if loop not in _notifiers:
        url = get_settings().database_url.replace("+psycopg", "")
        _notifiers[loop] = LiveNotifier(url)
    return _notifiers[loop]


async def shutdown() -> None:
    current = _notifiers.pop(id(asyncio.get_running_loop()), None)
    if current is not None:
        await current.close()
