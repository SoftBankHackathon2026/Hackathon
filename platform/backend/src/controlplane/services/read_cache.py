"""Short-lived cache for provider reads (monitoring, logs).

The monitoring screens poll every few seconds, often from several tabs, and each
read calls a cloud API. Identical reads within `ttl` share one result, and
concurrent identical reads await the one in flight instead of calling the cloud
again. Failures and empty (None) results are not cached; waiters of a failed
read get its error.

The cache lives on the event loop: waiting requests hold no worker thread, and
the shared read keeps running when the request that started it disconnects.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Hashable
from typing import Any


class ReadCache:
    def __init__(self, *, max_entries: int = 256, clock: Callable[[], float] = time.monotonic):
        self._entries: dict[Hashable, tuple[float, Any]] = {}
        self._inflight: dict[Hashable, asyncio.Future] = {}
        self._max = max_entries
        self._clock = clock

    async def get(self, key: Hashable, ttl: float, compute: Callable[[], Awaitable[Any]]) -> Any:
        entry = self._entries.get(key)
        if entry and entry[0] > self._clock():
            return entry[1]
        flight = self._inflight.get(key)
        if flight is None:
            flight = self._inflight[key] = asyncio.ensure_future(self._fill(key, ttl, compute))
            # Every waiter may have gone away; the error is then nobody's to handle.
            flight.add_done_callback(lambda done: done.cancelled() or done.exception())
        return await asyncio.shield(flight)

    def forget(self, match: Callable[[Hashable], bool]):
        """Drop matching results. Reads in flight finish for their waiters but are
        neither joined by later requests nor stored. Call on the event loop."""
        self._entries = {k: v for k, v in self._entries.items() if not match(k)}
        self._inflight = {k: v for k, v in self._inflight.items() if not match(k)}

    async def _fill(self, key, ttl, compute):
        me = asyncio.current_task()
        try:
            value = await compute()
        finally:
            current = self._inflight.get(key) is me
            if current:
                del self._inflight[key]
        if value is None or not current:
            return value
        # Keys include free-text log filters, so expired results are dropped on
        # every insert instead of staying until the cache is full.
        now = self._clock()
        self._entries = {k: v for k, v in self._entries.items() if v[0] > now}
        if len(self._entries) >= self._max:
            self._entries.clear()
        self._entries[key] = (now + ttl, value)
        return value
