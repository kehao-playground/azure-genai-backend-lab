"""Mutual exclusion per key, with a registry that empties itself.

Two places in this project need "one operation at a time for this id, others
in parallel": conversation turns, and the replacement of one document's
chunks. The naive version — ``dict[str, asyncio.Lock]``, entries created on
demand — is a memory leak with a public trigger, because nothing ever removes
an entry and the keys come from outside. Ten thousand ids probed once leave
ten thousand locks behind, forever.

So entries are reference-counted: one reference per holder *and* per waiter,
and the entry disappears when the last of them is done. The reference is
taken **before** waiting and dropped on the way out of either exit — the
normal one and cancellation. Getting that second path wrong is worse than the
leak it fixes: a waiter cancelled in the queue must drop its reference without
releasing a lock it never held, and must not delete an entry another task is
still holding. Both would silently let two operations into the same critical
section, and the corruption that follows names neither this file nor the lock.

The scope of this lock is **one process**. It is the right tool for a
single-worker lab and the wrong one for a deployment with replicas, which
needs a durable lease or a compare-and-set on a version the store owns.
"""

import asyncio
from collections.abc import AsyncIterator, Hashable
from contextlib import asynccontextmanager


class _Entry:
    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.refs = 0


class KeyedLock[K: Hashable]:
    """A lock per key, existing only while someone holds or awaits it.

    ``K`` is any hashable key: a plain ``str`` conversation id, or a
    composite tuple such as ``(tenant_id, conversation_id)`` — the entry
    registry only ever compares keys for equality, so a tuple key behaves
    exactly like a string one.
    """

    def __init__(self) -> None:
        self._entries: dict[K, _Entry] = {}

    def __len__(self) -> int:
        """Keys with a live entry — zero once every operation has finished."""
        return len(self._entries)

    def holders(self, key: K) -> int:
        """Holders plus waiters for ``key``; zero if it has no entry at all."""
        entry = self._entries.get(key)
        return entry.refs if entry is not None else 0

    def is_held(self, key: K) -> bool:
        entry = self._entries.get(key)
        return entry is not None and entry.lock.locked()

    async def acquire(self, key: K) -> None:
        entry = self._entries.get(key)
        if entry is None:
            entry = _Entry()
            self._entries[key] = entry
        # Counted before the await, so the entry cannot be reclaimed out from
        # under a task that is queued for it but has not acquired it yet.
        entry.refs += 1
        try:
            await entry.lock.acquire()
        except BaseException:
            # A waiter cancelled in the queue never reaches its caller's
            # release(): drop the reference here, and release nothing —
            # asyncio hands the lock to the next waiter on this path, and
            # calling release() as well would unlock it a second time.
            entry.refs -= 1
            if entry.refs == 0:
                del self._entries[key]
            raise

    def release(self, key: K) -> None:
        entry = self._entries[key]
        entry.lock.release()
        entry.refs -= 1
        if entry.refs == 0:
            del self._entries[key]

    @asynccontextmanager
    async def hold(self, key: K) -> AsyncIterator[None]:
        """Acquire for the duration of a block.

        Callers whose acquire and release sit in different frames — an
        exclusion that has to outlive the function that opened it — use
        ``acquire``/``release`` directly instead.
        """
        await self.acquire(key)
        try:
            yield
        finally:
            self.release(key)


__all__ = ["KeyedLock"]
