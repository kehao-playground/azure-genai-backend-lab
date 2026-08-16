"""Day 24: structured shutdown markers (D7).

An operator watching a live Container Apps revision deactivation has only the
container's own log stream to correlate against the platform's SIGTERM
record. These three INFO lines are that correlation anchor -- they must be
emitted exactly once each on a normal shutdown, with the third carrying the
measured elapsed time `_close_with_budget` actually spent, not merely "some
non-negative number".
"""

import logging

import pytest
from fastapi import FastAPI


@pytest.fixture
def app_with_lifespan() -> object:
    """A fresh app (all-fake settings, headers auth mode -- conftest.py pins
    both) driven through its real lifespan the same way test_service_lifecycle.py
    already does: `app.router.lifespan_context(app)` is the ASGI lifespan
    context manager itself, so `async with` on it runs startup then, on exit,
    the real `_lifespan` `finally` block this task instruments.
    """
    from azgenai_lab.main import create_app

    app: FastAPI = create_app()
    return app.router.lifespan_context(app)


async def test_normal_shutdown_emits_all_three_markers(
    caplog: pytest.LogCaptureFixture, app_with_lifespan: object
) -> None:
    caplog.set_level(logging.INFO, logger="azgenai_lab.main")
    async with app_with_lifespan:  # startup + clean shutdown, all-fake settings
        pass
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("lifespan shutdown started") for m in messages)
    assert any(m.startswith("shutdown cleanup started budget_seconds=") for m in messages)
    finished = [m for m in messages if m.startswith("shutdown cleanup finished elapsed_seconds=")]
    assert len(finished) == 1
    elapsed = float(finished[0].split("elapsed_seconds=")[1])
    assert elapsed >= 0.0
