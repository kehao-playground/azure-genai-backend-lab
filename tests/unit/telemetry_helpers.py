"""Shared telemetry test fixtures and span-tree assertions (Day 27).

Registered as a plugin from `tests/conftest.py`, not imported: pytest rejects
`pytest_plugins` in any non-rootdir conftest outright, and six test modules
need the same in-memory exporter wiring.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tests.conftest import AUTH_HEADERS

_CONNECTION_STRING = "InstrumentationKey=00000000-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    # `telemetry_enabled` mutates the environment `Settings` reads, and
    # get_settings is lru_cached: without clearing on both sides, one test's
    # connection string leaks into the next test's app.
    from azgenai_lab.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def span_exporter(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Set the module global directly rather than via set_tracer_provider(),
    # which refuses to replace an already-set provider and only logs about it.
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", provider, raising=False)
    yield exporter
    exporter.clear()


@pytest.fixture
def telemetry_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `configure_telemetry` install, without reaching Azure."""
    monkeypatch.setattr("azgenai_lab.core.telemetry._installed", False)
    monkeypatch.setattr(
        "azgenai_lab.core.telemetry.configure_azure_monitor", lambda **kwargs: None
    )
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", _CONNECTION_STRING)
    for key in (
        "OTEL_LOGS_EXPORTER",
        "OTEL_METRICS_EXPORTER",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def telemetry_app(telemetry_enabled: None) -> FastAPI:
    """A freshly built app, which span tests cannot do without.

    `tests/conftest.py` imports a module-level `app` built at import time --
    long before any telemetry fixture runs -- so the shared `client` fixture
    exercises an app that was never instrumented.
    """
    from azgenai_lab.main import create_app

    return create_app()


@pytest.fixture
def telemetry_client(telemetry_app: FastAPI) -> Iterator[TestClient]:
    # Same identity headers the shared `client` fixture carries: the four
    # protected endpoints have returned 401 without them since Day 15, and a
    # bare TestClient would be asserting the shape of a rejection.
    with TestClient(telemetry_app, headers=AUTH_HEADERS) as test_client:
        yield test_client
    telemetry_app.dependency_overrides.clear()


@dataclass
class SpanNode:
    """Defined before `span_tree` annotates with it: this module has no
    `from __future__ import annotations`, so a forward reference would be a
    NameError at import rather than a deferred lookup."""

    name: str
    span: object
    children: list[int] = field(default_factory=list)


def span_tree(exporter: InMemorySpanExporter) -> dict[int, SpanNode]:
    """Index spans by span_id, with the name as a field rather than the key.

    Keyed by name it would silently merge the two `chat {deployment}` spans a
    single agent run produces -- the exact multiplicity some of these tests
    exist to assert.
    """
    by_id = {span.context.span_id: span for span in exporter.get_finished_spans()}
    nodes = {
        span_id: SpanNode(name=span.name, span=span) for span_id, span in by_id.items()
    }
    for span_id, span in by_id.items():
        if span.parent is not None and span.parent.span_id in nodes:
            nodes[span.parent.span_id].children.append(span_id)
    return nodes


def children_named(
    nodes: dict[int, SpanNode], parent_prefix: str, child_prefix: str
) -> list[str]:
    """Names of every child of the one span matching `parent_prefix` whose own
    name starts with `child_prefix`.

    Returns a list, not a set: two children with the same name is a fact this
    has to be able to express.
    """
    parents = [node for node in nodes.values() if node.name.startswith(parent_prefix)]
    assert len(parents) == 1, [node.name for node in nodes.values()]
    return [
        nodes[child_id].name
        for child_id in parents[0].children
        if nodes[child_id].name.startswith(child_prefix)
    ]


def attribute_names(exporter: InMemorySpanExporter) -> set[str]:
    names: set[str] = set()
    for span in exporter.get_finished_spans():
        names.update(span.attributes or {})
        for event in span.events:
            names.update(event.attributes or {})
    return names
