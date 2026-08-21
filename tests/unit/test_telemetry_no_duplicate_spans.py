"""Day 27, check 1: one app start produces exactly one server span.

In-process this cannot be proven. The distro's automatic instrumentation runs
inside `configure_azure_monitor()` and patches module-level state that other
tests in this session have already touched, so a green result here would be a
statement about test ordering rather than about the shipped composition. A
fresh interpreter is the only place the question has a stable answer.

What it guards: the composition disables the distro's fastapi instrumentation
and instruments the app instance itself. If that disable were dropped, the two
mechanisms could stack -- and "it happens not to double right now" is not
something to rely on for a combination the distro does not support.
"""

import json
import os
import subprocess
import sys
import textwrap

_PROGRAM = textwrap.dedent(
    """
    import json

    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    import azgenai_lab.core.telemetry as telemetry

    # Stub the exporter side only: the point is the instrumentation wiring,
    # and no test should need a connection to Azure to answer this.
    telemetry.configure_azure_monitor = lambda **kwargs: None

    from fastapi.testclient import TestClient

    from azgenai_lab.main import create_app

    app = create_app()
    with TestClient(app, headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"}) as client:
        client.get("/api/v1/does-not-exist")

    print(
        json.dumps(
            [
                {"name": span.name, "kind": span.kind.name}
                for span in exporter.get_finished_spans()
            ]
        )
    )
    """
)


def test_exactly_one_server_span_per_request() -> None:
    env = dict(os.environ)
    env.update(
        {
            "APPLICATIONINSIGHTS_CONNECTION_STRING": (
                "InstrumentationKey=00000000-0000-0000-0000-000000000000"
            ),
            "USE_FAKE_LLM": "true",
            "USE_FAKE_SEARCH": "true",
            "USE_FAKE_EMBEDDINGS": "true",
            "AUTH_MODE": "headers",
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", _PROGRAM],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    spans = json.loads(result.stdout.strip().splitlines()[-1])
    # By kind, not by name prefix: an unmatched route has no template, so the
    # ASGI instrumentation names the span just "GET". Kind is what actually
    # says "this is the server span".
    server_spans = [span for span in spans if span["kind"] == "SERVER"]
    assert len(server_spans) == 1, spans
    # And no ASGI receive/send children leaked in either -- without
    # exclude_spans the documented tree stops being the only correct answer a
    # structure test can assert.
    assert [s for s in spans if s["name"] in {"http receive", "http send"}] == []
