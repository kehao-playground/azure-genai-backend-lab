"""Day 27: the telemetry composition point's environment contract.

The distro reads `os.environ` when it runs. This repo's settings go through
pydantic-settings, which populates `Settings` and never the process
environment -- so a value that lives only in `.env` is invisible to the
distro, logs and metrics keep being exported, and the only symptom is the
bill. These tests hold the three properties that follow from that: the keys
are written before the distro is called, a conflicting value fails closed
rather than being silently overwritten, and a failure leaves the environment
exactly as it found it.
"""

import os
from collections.abc import Iterator

import pytest

from azgenai_lab.core.config import Settings
from azgenai_lab.core.telemetry import (
    CONTROLLED_ENV,
    TelemetryConfigurationError,
    configure_telemetry,
)

_CONNECTION_STRING = "InstrumentationKey=00000000-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def _reset_installed(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Module-level latch: without resetting it, whichever test installs first
    # makes every later test's assertion about installation vacuous.
    monkeypatch.setattr("azgenai_lab.core.telemetry._installed", False)
    yield


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"applicationinsights_connection_string": None}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _enabled_settings() -> Settings:
    return _settings(applicationinsights_connection_string=_CONNECTION_STRING)


def test_no_connection_string_installs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[object] = []
    monkeypatch.setattr(
        "azgenai_lab.core.telemetry.configure_azure_monitor",
        lambda **kwargs: called.append(kwargs),
    )
    for key in CONTROLLED_ENV:
        monkeypatch.delenv(key, raising=False)

    assert configure_telemetry(_settings()) is False
    assert called == []
    # Not merely "no provider": a no-op run that still rewrote the process
    # environment would not be a no-op.
    for key in CONTROLLED_ENV:
        assert key not in os.environ


def test_controlled_env_written_before_configure(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str | None] = {}

    def _fake_configure(**kwargs: object) -> None:
        for key in CONTROLLED_ENV:
            seen[key] = os.environ.get(key)

    monkeypatch.setattr("azgenai_lab.core.telemetry.configure_azure_monitor", _fake_configure)
    for key in CONTROLLED_ENV:
        monkeypatch.delenv(key, raising=False)

    assert configure_telemetry(_enabled_settings()) is True
    # Read at the moment of the call, not afterwards: "set eventually" is not
    # the property that matters to something reading os.environ as it runs.
    assert seen == CONTROLLED_ENV


@pytest.mark.parametrize("conflicting_key", sorted(CONTROLLED_ENV))
def test_conflicting_env_fails_closed(
    monkeypatch: pytest.MonkeyPatch, conflicting_key: str
) -> None:
    called: list[object] = []
    monkeypatch.setattr(
        "azgenai_lab.core.telemetry.configure_azure_monitor",
        lambda **kwargs: called.append(kwargs),
    )
    for key in CONTROLLED_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(conflicting_key, "otlp")
    before = {key: os.environ.get(key) for key in CONTROLLED_ENV}

    with pytest.raises(TelemetryConfigurationError) as excinfo:
        configure_telemetry(_enabled_settings())

    message = str(excinfo.value)
    # The key and its actual value, both visible. core.errors.ConfigurationError
    # could not do this: UpstreamError.__init__ passes its own fixed message to
    # super(), so the detail would only ever reach the log.
    assert conflicting_key in message
    assert "otlp" in message
    # Nothing installed, and no half-written environment: every controlled key
    # is exactly as it was, absent ones still absent.
    assert called == []
    assert {key: os.environ.get(key) for key in CONTROLLED_ENV} == before


def test_conflict_on_a_later_key_does_not_write_an_earlier_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The two-phase point, stated as a test rather than a comment. A loop that
    # validated and wrote as it went would have written every key before this
    # one before raising.
    keys = sorted(CONTROLLED_ENV)
    first, last = keys[0], keys[-1]
    monkeypatch.setattr("azgenai_lab.core.telemetry.configure_azure_monitor", lambda **kw: None)
    for key in CONTROLLED_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(last, "otlp")

    with pytest.raises(TelemetryConfigurationError):
        configure_telemetry(_enabled_settings())

    assert first not in os.environ


def test_matching_existing_value_is_not_a_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("azgenai_lab.core.telemetry.configure_azure_monitor", lambda **kw: None)
    for key, value in CONTROLLED_ENV.items():
        monkeypatch.setenv(key, value)

    assert configure_telemetry(_enabled_settings()) is True


def test_reentrant_installs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        "azgenai_lab.core.telemetry.configure_azure_monitor",
        lambda **kwargs: calls.append(kwargs),
    )
    for key in CONTROLLED_ENV:
        monkeypatch.delenv(key, raising=False)
    settings = _enabled_settings()

    assert configure_telemetry(settings) is True
    assert configure_telemetry(settings) is True
    # create_app() and tools/index_corpus.py both call this, and the unit
    # suite runs them in one process. A second provider would double every
    # span rather than fail loudly.
    assert len(calls) == 1


def test_traces_only_options_are_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "azgenai_lab.core.telemetry.configure_azure_monitor",
        lambda **kwargs: captured.update(kwargs),
    )
    for key in CONTROLLED_ENV:
        monkeypatch.delenv(key, raising=False)

    configure_telemetry(_enabled_settings())

    assert captured["enable_live_metrics"] is False
    assert captured["enable_performance_counters"] is False
    # Explicit, because the distro's own default is a rate-limited sampler at
    # 5 traces/second -- which is why a reader following along would otherwise
    # not find their one request.
    assert captured["sampling_ratio"] == 1.0
    # httpx is deliberately absent: it is not in the distro's supported list,
    # so naming it would manage nothing while reading as though it did.
    assert captured["instrumentation_options"] == {"fastapi": {"enabled": False}}
