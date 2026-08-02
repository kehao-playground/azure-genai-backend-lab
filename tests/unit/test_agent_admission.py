"""Tests for the Day 17 per-run admission counter (Task 9).

A slot is consumed atomically at admission time, before the tool body runs,
and is never released — including when the caller is cancelled in the
window between admission and the body running. These tests count
*admissions* (state.admitted / state.refused), not body executions: a test
that only counted body starts would pass against a broken
release-on-cancel implementation.
"""

import asyncio
import contextlib

from azgenai_lab.services.agent_framework import (
    REFUSAL_MESSAGE,
    AdmissionState,
    wrap_tools_with_admission,
)


def _tools_with_probe(started: list[int], gate: asyncio.Event | None = None):
    async def probe() -> str:
        """Probe tool."""
        started.append(1)
        if gate is not None:
            await gate.wait()
        return "ok"

    return [probe]


async def test_sequential_11th_refused() -> None:
    state = AdmissionState(limit=10)
    started: list[int] = []
    [tool] = wrap_tools_with_admission(_tools_with_probe(started), state, [])
    results = [await tool() for _ in range(11)]
    assert results[:10] == ["ok"] * 10
    assert results[10] == REFUSAL_MESSAGE
    assert len(started) == 10 and state.refused == 1


async def test_parallel_race_admits_exactly_limit() -> None:
    state = AdmissionState(limit=10)
    started: list[int] = []
    [tool] = wrap_tools_with_admission(_tools_with_probe(started), state, [])
    results = await asyncio.gather(*(tool() for _ in range(50)))
    # counters count ADMISSIONS (spec §2a), and the tool body count agrees
    assert state.admitted == 10 and state.refused == 40
    assert results.count("ok") == 10 and results.count(REFUSAL_MESSAGE) == 40
    assert len(started) == 10


async def test_concurrent_runs_are_isolated() -> None:
    # two runs = two fresh states; each admits its full limit, never combined
    a, b = AdmissionState(limit=10), AdmissionState(limit=10)
    sa: list[int] = []
    sb: list[int] = []
    [ta] = wrap_tools_with_admission(_tools_with_probe(sa), a, [])
    [tb] = wrap_tools_with_admission(_tools_with_probe(sb), b, [])
    await asyncio.gather(*(ta() for _ in range(10)), *(tb() for _ in range(10)))
    assert a.admitted == 10 and b.admitted == 10


async def test_cancellation_window_consumes_slot() -> None:
    # admitted, paused BEFORE the body's first line, cancelled there:
    # the slot stays consumed and the body-first-line counter may be 0.
    state = AdmissionState(limit=10)
    started: list[int] = []
    gate = asyncio.Event()

    async def paused_tool() -> str:
        """Pauses before its own first observable line via the wrapper hook."""
        started.append(1)
        return "ok"

    [tool] = wrap_tools_with_admission(
        [paused_tool], state, [], _test_pause_after_admit=gate
    )
    task = asyncio.create_task(tool())
    await asyncio.sleep(0)  # let it pass admission and hit the pause
    while state.admitted == 0:
        await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert state.admitted == 1  # slot consumed
    assert started == []  # body never started
    # the run can only admit 9 more
    [t2] = wrap_tools_with_admission(_tools_with_probe([]), state, [])
    results = [await t2() for _ in range(10)]
    assert results.count(REFUSAL_MESSAGE) == 1


async def test_body_exception_never_releases_slot() -> None:
    state = AdmissionState(limit=1)

    async def boom() -> str:
        """Raises."""
        raise RuntimeError("boom")

    [tool] = wrap_tools_with_admission([boom], state, [])
    with contextlib.suppress(RuntimeError):
        await tool()
    assert state.admitted == 1
    assert await tool() == REFUSAL_MESSAGE  # limit=1: next call refused
