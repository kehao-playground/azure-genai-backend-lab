"""Telemetry composition point (Day 27).

One function, called once per entrypoint -- `create_app()` and
`tools/index_corpus.py` -- and reentrant, so a process that runs both installs
one provider rather than two.

Why the process environment is written here rather than left to `.env`: the
Azure Monitor distro reads `os.environ` when it runs, and this repo's settings
go through pydantic-settings, which populates `Settings` and never the process
environment. A value placed in `.env` alone is therefore never seen by the
distro -- logs and metrics keep being exported, no error is raised, and the
only symptom is the bill.

What this milestone ships is traces. Logs stay where they were (stderr, then
whatever the hosting log pipeline does with them -- Container Apps already
collects them), which keeps Day 22's statement that the audit trail is a JSON
line on a process log stream true word for word, and keeps the same lines from
being ingested and billed twice.
"""

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
from agent_framework.observability import enable_instrumentation
from azure.monitor.opentelemetry import configure_azure_monitor
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from azgenai_lab.core.config import Settings
from azgenai_lab.core.correlation import ATTR_CORRELATION_ID, correlation_id_var
from azgenai_lab.models.chat import TokenUsage

logger = logging.getLogger(__name__)

# The values are OTel's environment-variable spelling: the string "none", not
# Python's None.
#
# The content switch is set rather than left unset on purpose. "Unset" is an
# assumption that an upstream default will not change; this key decides
# whether prompt and completion text is copied into a second store, which is a
# data-governance decision and belongs in the code that owns it. Note this is
# only one of the two content switches -- agent-framework has its own
# (ENABLE_SENSITIVE_DATA), disabled programmatically in `configure_telemetry`
# because its settings singleton is built at import time.
CONTROLLED_ENV: dict[str, str] = {
    "OTEL_LOGS_EXPORTER": "none",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "false",
}

# `excluded_urls` is a comma-separated list of regexes joined with "|" and
# matched with re.search against the *full* URL
# (opentelemetry/util/http/__init__.py:74-83). A bare "health" would therefore
# also swallow /api/v1/healthz, any request whose query string mentions health,
# and a host named health.example -- so the pattern is anchored, and a negative
# test covers the URLs it must still trace.
#
# The exclusion exists because Container Apps runs startup, liveness and
# readiness probes against GET /health, liveness every 10 seconds
# (docs/container-apps.md). At sampling_ratio 1.0 those would dominate the
# data, and this milestone is about the lifecycle of one LLM request.
HEALTH_EXCLUDED_URLS = r"^https?://[^/]+/health$"

# Own namespace, closed value set, low cardinality on purpose. This one is a
# metric-shaped dimension even though it lives on a span, and a free-form
# upstream status string here is how dimension explosions start. The error code
# is read off UpstreamError rather than mapped again: Day 22 already closed
# that set in the type, and a second copy of it would drift.
ATTR_OUTCOME = "azgenai.outcome"
ATTR_ERROR_CODE = "azgenai.error.code"
OUTCOME_SUCCESS = "success"
OUTCOME_REJECTED = "rejected"
OUTCOME_ERROR = "error"

# Time to first chunk, in seconds, measured from request issuance -- the
# semantic convention's own definition
# (opentelemetry/semconv/_incubating/attributes/gen_ai_attributes.py:271),
# which is why the owning span starts before the request goes out rather than
# when the adapter hands back a stream. Imported rather than spelled out so an
# upstream rename fails at import instead of silently emitting a dead key; it
# lives under `_incubating`, so that is a real possibility.
ATTR_TTFB = gen_ai_attributes.GEN_AI_RESPONSE_TIME_TO_FIRST_CHUNK
# Re-exported from the same place as the rest so callers have one import for
# the vocabulary, and an upstream rename surfaces here rather than in five
# adapters.
ATTR_FINISH_REASONS = gen_ai_attributes.GEN_AI_RESPONSE_FINISH_REASONS

# What a fake adapter reports as its deployment. The span name has to be
# decided before the call, so it cannot be read off the result -- and Day 22
# set the precedent of a "fake" sentinel rather than a blank or a lie
# (provider_call_attempted records that the adapter boundary was reached, not
# that Azure was).
FAKE_DEPLOYMENT = "fake"
FAKE_EMBEDDING_DEPLOYMENT = "fake-embeddings"

_installed = False


class TelemetryConfigurationError(Exception):
    """A startup failure, deliberately not an `UpstreamError` subclass.

    `core.errors.ConfigurationError` exists to translate a backend failure into
    the client-facing envelope; at startup there is no request, no envelope and
    no correlation id to put in one. It also passes its own fixed ``message``
    to ``super().__init__``, keeping the caller's detail in ``upstream_detail``
    -- which that module documents as log-only -- so the offending key would
    never reach ``str(exc)``. That key is the one thing an operator needs here.
    """

    def __init__(self, conflicts: dict[str, str]) -> None:
        found = ", ".join(f"{key}={value!r}" for key, value in sorted(conflicts.items()))
        expected = ", ".join(f"{key}={value!r}" for key, value in sorted(CONTROLLED_ENV.items()))
        super().__init__(
            f"telemetry environment conflicts with this build's traces-only "
            f"configuration: found {found}; expected {expected}"
        )
        self.conflicts = conflicts


def _apply_controlled_env() -> None:
    """Validate every controlled key, then write them all.

    Two phases on purpose. A single loop that validated and wrote as it went
    would already have mutated key #1 by the time key #2 turned out to
    conflict, and the raise would leave the process in neither the original
    nor the target state -- which the next caller in the same process
    inherits.
    """
    conflicts = {
        key: actual
        for key, wanted in CONTROLLED_ENV.items()
        if (actual := os.environ.get(key)) is not None and actual != wanted
    }
    if conflicts:
        raise TelemetryConfigurationError(conflicts)
    os.environ.update(CONTROLLED_ENV)


def configure_telemetry(settings: Settings) -> bool:
    """Install the telemetry pipeline. Returns whether it was installed.

    No connection string means no provider, no patched client and no
    environment mutation -- the default posture for CI, local development and
    every ``USE_FAKE_*`` path. Callers use the return value to decide whether
    to instrument anything of their own: `FastAPIInstrumentor.instrument_app`
    patches the instance whether or not a provider exists, so "no provider" and
    "not instrumented" are different states and only the second one is a no-op.
    """
    global _installed
    if settings.applicationinsights_connection_string is None:
        return False
    if _installed:
        return True

    _apply_controlled_env()
    configure_azure_monitor(
        connection_string=settings.applicationinsights_connection_string.get_secret_value(),
        # httpx is deliberately absent. It is not in the distro's supported
        # instrumentation list, so naming it here would manage nothing while
        # reading as though the distro covered it -- and the distro covering
        # httpx is exactly the thing that is not true. Per-client
        # instrumentation is what actually covers it.
        instrumentation_options={"fastapi": {"enabled": False}},
        enable_live_metrics=False,
        enable_performance_counters=False,
        sampling_ratio=settings.telemetry_sampling_ratio,
        resource=Resource.create({SERVICE_NAME: settings.otel_service_name}),
    )
    # Programmatic, not environmental. agent-framework's OBSERVABILITY_SETTINGS
    # is a module-level singleton built at import time, so setting
    # ENABLE_SENSITIVE_DATA after that changes nothing -- and passing
    # enable_sensitive_data=None makes this function re-read the environment,
    # which hands the decision back to whoever set it. Instrumentation itself
    # stays enabled: /agent's spans (invoke_agent, chat, execute_tool) are the
    # framework's, by design.
    enable_instrumentation(enable_sensitive_data=False)
    _installed = True
    logger.info(
        "telemetry configured service_name=%s sampling_ratio=%s",
        settings.otel_service_name,
        settings.telemetry_sampling_ratio,
    )
    return True


def instrument_fastapi_app(app: FastAPI) -> None:
    """Instrument this app instance rather than the FastAPI class.

    The distro's automatic mode replaces ``fastapi.FastAPI`` in the fastapi
    module namespace, but `main.py` binds that name at import time and
    `create_app()` uses the bound reference -- so automatic instrumentation
    reaches nothing here and every server span silently disappears.
    Instrumenting the instance is independent of import order, which is why the
    automatic path is disabled outright rather than left on in the hope that it
    misses.

    Call order relative to middleware registration does not matter, which is
    worth stating because it is not obvious: `instrument_app` does not go
    through ``add_middleware`` at all. It replaces ``app.build_middleware_stack``
    with a wrapper that builds the user's stack and then wraps
    ``OpenTelemetryMiddleware`` around the outside of it
    (opentelemetry/instrumentation/fastapi/__init__.py:355-395), and that
    method runs lazily on startup. The server span therefore always encloses
    every user middleware, including `correlation_id_middleware`, however early
    or late this is called. Verified by moving the call before the middleware
    registration and watching the correlation-id span assertion stay green.
    """
    if not _installed:
        # Guarded here as well as at the call site: tools/index_corpus.py is a
        # second entrypoint and does not go through create_app()'s branch.
        return
    FastAPIInstrumentor.instrument_app(
        app,
        # Without this the ASGI instrumentation also emits `http send` and
        # `http receive` children, and the documented span tree stops being the
        # only correct answer a structure test can assert.
        exclude_spans=["receive", "send"],
        excluded_urls=HEALTH_EXCLUDED_URLS,
    )


def instrumented_httpx_client(**kwargs: Any) -> httpx.AsyncClient:
    """Build an httpx client, instrumented per client rather than globally.

    Every upstream call this service makes travels over httpx -- the openai SDK
    included -- and httpx is not in the distro's bundled instrumentation list,
    so without this the dependency half of a request's lifecycle is empty.

    Per client rather than `HTTPXClientInstrumentor().instrument()` for two
    reasons: which clients are traced stays answerable by reading the
    composition point, and a global patch stacked on top of a per-client one is
    a combination whose behaviour is not specified anywhere.
    """
    client = httpx.AsyncClient(**kwargs)
    if _installed:
        HTTPXClientInstrumentor.instrument_client(client)
    return client


@contextmanager
def llm_span(deployment: str, *, operation: str = "chat") -> Iterator[Span]:
    """A semantic span over one model call.

    Named ``{operation} {deployment}`` to match what agent-framework emits on
    the /agent path, so a reader comparing the two trees is comparing like with
    like. There are genuinely two producers of this span -- the framework's on
    /agent, ours on /chat and /rag, because those go through the openai SDK
    directly -- and their looking alike is a decision, not a coincidence.
    """
    with trace.get_tracer("azgenai_lab").start_as_current_span(
        f"{operation} {deployment}",
        kind=SpanKind.CLIENT,
        # Both off, deliberately. record_exception defaults to True and writes
        # the exception's own message into a span event -- upstream detail,
        # verbatim, straight past the never-log rules Day 15/19/21/22 built up.
        # Status is set explicitly by set_outcome instead. agent-framework's
        # get_function_span turns the same two off for the same reason.
        record_exception=False,
        set_status_on_exception=False,
        attributes={
            gen_ai_attributes.GEN_AI_OPERATION_NAME: operation,
            gen_ai_attributes.GEN_AI_PROVIDER_NAME: "azure.ai.openai",
            gen_ai_attributes.GEN_AI_REQUEST_MODEL: deployment,
        },
    ) as span:
        correlation_id = correlation_id_var.get()
        if correlation_id is not None:
            span.set_attribute(ATTR_CORRELATION_ID, correlation_id)
        yield span


@contextmanager
def stage_span(name: str) -> Iterator[Span]:
    """An application-owned span over one pipeline stage."""
    with trace.get_tracer("azgenai_lab").start_as_current_span(
        name,
        kind=SpanKind.INTERNAL,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        correlation_id = correlation_id_var.get()
        if correlation_id is not None:
            span.set_attribute(ATTR_CORRELATION_ID, correlation_id)
        yield span


def set_usage_attributes(span: Span, usage: TokenUsage | None) -> None:
    """Record provider-reported tokens. Absent usage leaves the keys absent.

    Writing 0 for an unknown would turn "we do not know" into "it cost
    nothing", and Day 9 already disclosed that a failed or disconnected call
    may have incurred billable processing upstream while reporting no usage at
    all.
    """
    if usage is None:
        return
    span.set_attribute(gen_ai_attributes.GEN_AI_USAGE_INPUT_TOKENS, usage.input_tokens)
    span.set_attribute(gen_ai_attributes.GEN_AI_USAGE_OUTPUT_TOKENS, usage.output_tokens)


def set_outcome(span: Span, outcome: str, error_code: str | None = None) -> None:
    span.set_attribute(ATTR_OUTCOME, outcome)
    if error_code is not None:
        span.set_attribute(ATTR_ERROR_CODE, error_code)
    if outcome == OUTCOME_ERROR:
        # The classification, never the upstream's own message.
        span.set_status(Status(StatusCode.ERROR, error_code or OUTCOME_ERROR))
