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

from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource

from azgenai_lab.core.config import Settings

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
    _installed = True
    logger.info(
        "telemetry configured service_name=%s sampling_ratio=%s",
        settings.otel_service_name,
        settings.telemetry_sampling_ratio,
    )
    return True
