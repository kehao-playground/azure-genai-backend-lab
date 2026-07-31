"""Mutual exclusion per key, and a registry that goes back to empty.

The registry is the easy half: assert it is empty when nothing is running.
The hard half is the exits — a failed operation, a cancelled waiter — because
a reference dropped twice reclaims an entry another task is still holding,
and the next caller for that key then gets a *different* lock and walks
straight into the critical section beside the holder. That failure is silent
at the lock and loud somewhere else entirely, so it is pinned here directly.
"""

import asyncio

import pytest

from azgenai_lab.core.keyed_lock import KeyedLock


async def test_one_key_admits_one_holder_at_a_time() -> None:
    locks = KeyedLock()
    inside: list[str] = []

    async def job(name: str) -> None:
        async with locks.hold("k"):
            inside.append(f"enter-{name}")
            await asyncio.sleep(0)  # a suspension point unlocked jobs interleave at
            inside.append(f"exit-{name}")

    await asyncio.gather(job("a"), job("b"))

    # Serialized, in whichever order: never enter, enter, exit, exit.
    assert inside in (
        ["enter-a", "exit-a", "enter-b", "exit-b"],
        ["enter-b", "exit-b", "enter-a", "exit-a"],
    )


async def test_different_keys_do_not_wait_for_each_other() -> None:
    locks = KeyedLock()
    order: list[str] = []

    async def job(key: str) -> None:
        async with locks.hold(key):
            order.append(f"enter-{key}")
            await asyncio.sleep(0)
            order.append(f"exit-{key}")

    await asyncio.gather(job("a"), job("b"))

    # Both entered before either left: a single global lock would serialize
    # them and produce the same shape as the test above.
    assert order == ["enter-a", "enter-b", "exit-a", "exit-b"]


async def test_the_registry_is_empty_once_nothing_is_running() -> None:
    locks = KeyedLock()

    async def job(key: str) -> None:
        async with locks.hold(key):
            await asyncio.sleep(0)

    await asyncio.gather(*(job(f"k{n}") for n in range(2000)))

    assert len(locks) == 0


async def test_a_waiter_keeps_the_entry_alive_and_then_releases_it() -> None:
    locks = KeyedLock()
    await locks.acquire("k")
    waiter = asyncio.create_task(locks.acquire("k"))
    await asyncio.sleep(0)  # let the waiter enqueue

    assert locks.holders("k") == 2

    locks.release("k")  # hands the lock to the waiter
    await waiter
    assert locks.holders("k") == 1
    assert locks.is_held("k") is True

    locks.release("k")
    assert len(locks) == 0
    assert locks.holders("k") == 0


async def test_a_failed_operation_still_releases_and_reclaims() -> None:
    locks = KeyedLock()

    with pytest.raises(RuntimeError):
        async with locks.hold("k"):
            raise RuntimeError("the job failed")

    assert len(locks) == 0
    # And the key is genuinely free afterwards, not merely absent from the
    # registry while its lock stayed held.
    async with locks.hold("k"):
        assert locks.is_held("k") is True


async def test_a_cancelled_waiter_drops_only_its_own_reference() -> None:
    # The dangerous direction: a waiter cancelled in the queue must not
    # release a lock it never acquired, and must not delete an entry the
    # holder is still using. Either one lets the next caller for this key
    # acquire a *different* lock object and enter beside the holder.
    locks = KeyedLock()
    await locks.acquire("k")
    waiter = asyncio.create_task(locks.acquire("k"))
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert locks.holders("k") == 1
    assert locks.is_held("k") is True

    # The holder's exclusion still holds against a fresh arrival: this task
    # must not get in until the holder releases.
    entered = False

    async def newcomer() -> None:
        nonlocal entered
        async with locks.hold("k"):
            entered = True

    second = asyncio.create_task(newcomer())
    await asyncio.sleep(0)
    assert entered is False, "a newcomer entered while the holder still held the lock"

    locks.release("k")
    await second
    assert entered is True
    assert len(locks) == 0


async def test_tuple_keys_are_independent_of_each_other() -> None:
    # Task 10: conversations are keyed (tenant_id, conversation_id). A lock
    # held for one tenant's "c" must not block another tenant's "c".
    locks: KeyedLock[tuple[str, str]] = KeyedLock()
    await locks.acquire(("t1", "c"))
    try:
        async with asyncio.timeout(0.1):
            await locks.acquire(("t2", "c"))
        locks.release(("t2", "c"))
    finally:
        locks.release(("t1", "c"))

    assert len(locks) == 0


async def test_the_last_of_several_cancelled_waiters_reclaims_the_entry() -> None:
    # Nobody ever holds this key: every acquirer is cancelled while queued
    # except the first, which is then released. If a cancelled waiter forgot
    # to reclaim on the way out, the entry would survive with no holder.
    locks = KeyedLock()
    await locks.acquire("k")
    waiters = [asyncio.create_task(locks.acquire("k")) for _ in range(3)]
    await asyncio.sleep(0)
    assert locks.holders("k") == 4

    for waiter in waiters:
        waiter.cancel()
    for waiter in waiters:
        with pytest.raises(asyncio.CancelledError):
            await waiter

    assert locks.holders("k") == 1
    locks.release("k")
    assert len(locks) == 0
