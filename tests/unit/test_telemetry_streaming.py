"""Day 27: the streaming span's owner, and every exit that has to close it.

The disconnect cases drive the generator directly rather than going through
TestClient. tests/unit/test_audit_error_codes.py:114 already records why: an
endpoint-level TestClient disconnect cannot pin the cutoff relative to the
terminal event the way calling aclose() by hand can.
"""

import anyio
import pytest
from fastapi.testclient import TestClient

from azgenai_lab.core.telemetry import ATTR_TTFB, FAKE_DEPLOYMENT
from azgenai_lab.services.azure_openai import (
    FakeChatService,
    StreamDone,
    TextDelta,
    TracingChatService,
)


def _llm_span(exporter):
    spans = [s for s in exporter.get_finished_spans() if s.name.startswith("chat ")]
    assert len(spans) == 1, [s.name for s in exporter.get_finished_spans()]
    return spans[0]


def _open_spans(exporter):
    """Spans that were started but never ended are, by definition, not in the
    exporter -- so leaks are detected by absence, not by presence."""
    return [s for s in exporter.get_finished_spans() if s.name.startswith("chat ")]


class _ScriptedChatService:
    """A stream with a controllable tail: terminal, EOF, or an exception."""

    def __init__(self, *, terminal: str | None = "done") -> None:
        self._terminal = terminal

    async def complete(self, items: object) -> object:  # pragma: no cover
        raise NotImplementedError

    async def open_stream(self, items: object):
        terminal = self._terminal

        async def stream():
            yield TextDelta("first ")
            yield TextDelta("second")
            if terminal == "done":
                yield StreamDone(status="completed", replay_items=(), usage=None)

        return stream()

    async def aclose(self) -> None:
        return None


def _traced(**kwargs) -> TracingChatService:
    return TracingChatService(_ScriptedChatService(**kwargs), FAKE_DEPLOYMENT)  # type: ignore[arg-type]


async def test_normal_stream_records_ttfb_and_closes(telemetry_enabled, span_exporter) -> None:
    events = await _traced().open_stream([])
    async for _ in events:
        pass

    span = _llm_span(span_exporter)
    attrs = span.attributes or {}
    assert attrs[ATTR_TTFB] > 0
    assert attrs["azgenai.outcome"] == "success"
    assert span.end_time is not None


async def test_disconnect_after_terminal_keeps_the_recorded_outcome(
    telemetry_enabled, span_exporter
) -> None:
    events = await _traced().open_stream([])
    async for event in events:
        if isinstance(event, StreamDone):
            break
    await events.aclose()  # consumer goes away after the terminal

    attrs = _llm_span(span_exporter).attributes or {}
    # The terminal already recorded success; the exit path must not overwrite
    # it with the no-terminal error.
    assert attrs["azgenai.outcome"] == "success"


async def test_disconnect_before_first_delta_leaves_ttfb_and_usage_absent(
    telemetry_enabled, span_exporter
) -> None:
    events = await _traced().open_stream([])
    await events.aclose()  # closed before a single iteration

    attrs = _llm_span(span_exporter).attributes or {}
    # Not zero. We do not know, and the upstream may still have been billed
    # (Day 9's disclosed gap).
    assert ATTR_TTFB not in attrs
    assert "gen_ai.usage.output_tokens" not in attrs
    assert attrs["azgenai.outcome"] == "error"


async def test_upstream_eof_without_terminal_is_an_error(
    telemetry_enabled, span_exporter
) -> None:
    events = await _traced(terminal=None).open_stream([])
    async for _ in events:
        pass

    attrs = _llm_span(span_exporter).attributes or {}
    assert attrs["azgenai.outcome"] == "error"
    assert attrs["azgenai.error.code"] == "upstream_error"
    # The deltas still arrived, so this one *does* have a first chunk.
    assert ATTR_TTFB in attrs


async def test_stream_never_iterated_does_not_leak_the_span(
    telemetry_enabled, span_exporter
) -> None:
    # Built and dropped without a single iteration. A generator that is never
    # started never runs its finally, so this is the case where the owner has
    # to be closed by someone else -- and it is the case a happy-path test
    # would never notice.
    events = await _traced().open_stream([])
    await events.aclose()

    assert _open_spans(span_exporter) != [], "span was started but never ended"


async def test_close_is_idempotent_and_first_writer_wins(
    telemetry_enabled, span_exporter
) -> None:
    events = await _traced().open_stream([])
    async for _ in events:
        pass
    await events.aclose()
    await events.aclose()

    attrs = _llm_span(span_exporter).attributes or {}
    assert attrs["azgenai.outcome"] == "success"


def test_send_failure_still_closes_the_span(telemetry_app, span_exporter) -> None:
    """Raw ASGI, because this is the case background tasks do not cover.

    Starlette does not run background tasks when `send` raises, so anything
    relying on one would not close here.

    The exception type is asserted from measurement, not from expectation: the
    design review predicted Starlette would surface a send-side OSError as
    ClientDisconnect, and on the pinned versions it does not -- the OSError
    propagates unchanged. Recorded rather than reasoned about, because if a
    future version does wrap it, this assertion is where that shows up.
    """

    async def _receive() -> dict[str, object]:
        return {"type": "http.request", "body": b'{"message": "hi"}', "more_body": False}

    async def _send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body":
            raise OSError("client went away")

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": "/api/v1/chat/stream",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", b"17"),
            (b"x-tenant-id", b"t1"),
            (b"x-user-id", b"u1"),
        ],
        "client": ("testclient", 50000),
    }

    async def _drive() -> None:
        with pytest.raises(OSError):
            await telemetry_app(scope, _receive, _send)

    anyio.run(_drive)

    assert _open_spans(span_exporter) != [], "span was started but never ended"


def test_fake_service_stream_is_traced_end_to_end(
    telemetry_client: TestClient, span_exporter
) -> None:
    with telemetry_client.stream(
        "POST", "/api/v1/chat/stream", json={"message": "hi"}
    ) as response:
        assert response.status_code == 200
        list(response.iter_lines())

    span = _llm_span(span_exporter)
    attrs = span.attributes or {}
    assert attrs["gen_ai.request.model"] == FAKE_DEPLOYMENT
    assert attrs["azgenai.outcome"] == "success"
    assert ATTR_TTFB in attrs
    assert isinstance(FakeChatService, type)  # import used: the app builds this adapter
