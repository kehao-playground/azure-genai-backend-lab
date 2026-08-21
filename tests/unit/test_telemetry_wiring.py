"""Day 27: the composition invariants, each with a silent failure mode.

Every assertion here corresponds to something that would otherwise go wrong
without an error: a content switch left to the environment, an app patched
when nothing is installed, probe traffic drowning the data, or a second
provider doubling every span.
"""

import os

import pytest
from agent_framework.observability import OBSERVABILITY_SETTINGS
from fastapi.testclient import TestClient
from tests.conftest import AUTH_HEADERS

from azgenai_lab.core.telemetry import CONTROLLED_ENV
from azgenai_lab.main import create_app


def test_sensitive_data_disabled_programmatically(telemetry_enabled: None) -> None:
    create_app()
    # Read back the singleton, not the environment. OBSERVABILITY_SETTINGS is
    # built at import time, so setting ENABLE_SENSITIVE_DATA afterwards changes
    # nothing -- and passing enable_sensitive_data=None makes the framework
    # re-read the environment, handing the decision back to whoever set it.
    assert OBSERVABILITY_SETTINGS.enable_sensitive_data is False


def test_framework_instrumentation_stays_enabled(telemetry_enabled: None) -> None:
    create_app()
    # /agent's spans come from the framework by design (invoke_agent, chat,
    # execute_tool). Disabling it would silently empty that tree.
    assert OBSERVABILITY_SETTINGS.enable_instrumentation is True


def test_genai_content_capture_is_false(telemetry_enabled: None) -> None:
    create_app()
    assert os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "false"
    assert CONTROLLED_ENV["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "false"


def test_health_produces_no_server_span(telemetry_app, span_exporter) -> None:
    with TestClient(telemetry_app) as client:  # /health is unprotected
        assert client.get("/health").status_code == 200
    assert span_exporter.get_finished_spans() == ()


def test_non_health_url_containing_health_still_produces_a_span(
    telemetry_app, span_exporter
) -> None:
    # The negative half of the exclusion. excluded_urls runs a regex search
    # over the full URL, so an unanchored "health" would also swallow this one.
    with TestClient(telemetry_app, headers=AUTH_HEADERS) as client:
        client.get("/api/v1/healthz-not-a-real-route")
    assert [span.name for span in span_exporter.get_finished_spans()] != []


def test_no_connection_string_patches_nothing(
    monkeypatch: pytest.MonkeyPatch, span_exporter
) -> None:
    # The posture CI and every other test in this suite run under.
    # instrument_app() patches the instance whether or not a provider exists,
    # so "no provider installed" and "not instrumented" are different states
    # and only the second one leaves the default path untouched.
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    monkeypatch.setattr("azgenai_lab.core.telemetry._installed", False)
    calls: list[object] = []
    monkeypatch.setattr(
        "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app",
        staticmethod(lambda *args, **kwargs: calls.append(args)),
    )

    app = create_app()
    with TestClient(app, headers=AUTH_HEADERS) as client:
        client.get("/health")

    assert calls == []
    assert span_exporter.get_finished_spans() == ()
