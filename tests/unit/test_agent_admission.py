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

import pytest

from azgenai_lab.services.agent_framework import (
    REFUSAL_MESSAGE,
    AdmissionState,
    ToolExecution,
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
    executions: list[ToolExecution] = []
    [tool] = wrap_tools_with_admission(_tools_with_probe(started), state, executions)
    results = [await tool() for _ in range(11)]
    assert results[:10] == ["ok"] * 10
    assert results[10] == REFUSAL_MESSAGE
    assert len(started) == 10 and state.refused == 1
    # sink: one executed record per admitted call, one refusal record for the 11th
    assert len(executions) == 11
    executed_record = executions[0]
    assert executed_record.tool_name == "probe"
    assert executed_record.executed is True
    assert executed_record.latency_ms >= 0.0
    refusal_record = executions[10]
    assert refusal_record.tool_name == "probe"
    assert refusal_record.executed is False
    assert refusal_record.latency_ms == 0.0


async def test_parallel_race_admits_exactly_limit() -> None:
    state = AdmissionState(limit=10)
    started: list[int] = []
    executions: list[ToolExecution] = []
    [tool] = wrap_tools_with_admission(_tools_with_probe(started), state, executions)
    results = await asyncio.gather(*(tool() for _ in range(50)))
    # counters count ADMISSIONS (spec §2a), and the tool body count agrees
    assert state.admitted == 10 and state.refused == 40
    assert results.count("ok") == 10 and results.count(REFUSAL_MESSAGE) == 40
    assert len(started) == 10
    # sink: a regression that flips executed on refusal (or vice versa) would
    # be invisible to the counters above but shows up here
    assert len(executions) == 50
    assert sum(1 for e in executions if e.executed) == 10
    assert sum(1 for e in executions if not e.executed) == 40
    assert all(e.tool_name == "probe" for e in executions)


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

    async def _wait_for_admission() -> None:
        while state.admitted == 0:
            await asyncio.sleep(0)

    # bounded: against a broken implementation that never admits, this must
    # fail the test instead of hanging the suite
    await asyncio.wait_for(_wait_for_admission(), timeout=1.0)
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


async def test_body_exception_still_records_execution() -> None:
    # a raising body must not be invisible to the execution sink: later tasks
    # (run trace, per-tool latency) read this list, and a search failure
    # (services/agent_tools.py has zero except clauses) must show up here.
    state = AdmissionState(limit=1)
    executions: list[ToolExecution] = []

    async def boom() -> str:
        """Raises."""
        raise RuntimeError("boom")

    [tool] = wrap_tools_with_admission([boom], state, executions)
    with pytest.raises(RuntimeError, match="boom"):
        await tool()
    assert state.admitted == 1  # unchanged by the failure: slot stays consumed
    assert len(executions) == 1
    record = executions[0]
    assert record.tool_name == "boom"
    assert record.executed is True  # the slot was consumed and the body ran
    assert record.latency_ms >= 0.0


async def test_body_cancellation_still_records_execution() -> None:
    # cancellation landing *inside* the running body (as opposed to the
    # admission-to-body-start window covered by
    # test_cancellation_window_consumes_slot) must also produce a record.
    state = AdmissionState(limit=1)
    executions: list[ToolExecution] = []
    body_started = asyncio.Event()

    async def hangs() -> str:
        """Starts, then hangs until cancelled."""
        body_started.set()
        await asyncio.Event().wait()  # never set: only cancellation ends this
        return "unreachable"

    [tool] = wrap_tools_with_admission([hangs], state, executions)
    task = asyncio.create_task(tool())
    await asyncio.wait_for(body_started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state.admitted == 1  # unchanged by the cancellation
    assert len(executions) == 1
    record = executions[0]
    assert record.tool_name == "hangs"
    assert record.executed is True
    assert record.latency_ms >= 0.0
