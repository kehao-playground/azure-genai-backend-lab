"""Startup must actually configure logging: `configure_logging` existing but
uncalled means every INFO log — including the per-LLM-call prompt-identity
line — is silently dropped under a plain `uvicorn` run (no pytest logging
capture, no basicConfig from elsewhere)."""

import logging

import pytest

from azgenai_lab.core.config import Settings
from azgenai_lab.core.logging import configure_logging
from azgenai_lab.core.tenant_context import tenant_id_var, user_id_var

# Same format string as configure_logging() — duplicated here (rather than
# imported) because the point of the test below is to catch the format
# string silently drifting away from what the record factory populates.
LOG_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s "
    "correlation_id=%(correlation_id)s tenant_id=%(tenant_id)s "
    "user_id=%(user_id)s %(message)s"
)


def test_configure_logging_sets_root_level_to_given_level() -> None:
    try:
        configure_logging("WARNING")
        assert logging.getLogger().getEffectiveLevel() == logging.WARNING

        configure_logging("DEBUG")
        assert logging.getLogger().getEffectiveLevel() == logging.DEBUG
    finally:
        # Don't leak a non-default root level into other tests.
        configure_logging("WARNING")


def test_settings_log_level_defaults_to_info_and_honors_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    assert Settings(_env_file=None).log_level == "INFO"

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    assert Settings(_env_file=None).log_level == "DEBUG"


def test_create_app_wires_root_logger_to_settings_log_level(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("USE_FAKE_LLM", "true")

    from azgenai_lab.core.config import get_settings
    from azgenai_lab.main import create_app

    get_settings.cache_clear()
    try:
        create_app()
        assert logging.getLogger().getEffectiveLevel() == logging.DEBUG
    finally:
        get_settings.cache_clear()
        configure_logging("WARNING")


def test_identity_fields_are_rendered_not_just_attached(
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_logging("INFO")
    formatter = logging.Formatter(LOG_FORMAT)
    token_t = tenant_id_var.set("t1")
    token_u = user_id_var.set("u1")
    try:
        record = logging.getLogger("test").makeRecord(
            "test", logging.INFO, __file__, 1, "identity resolved", None, None
        )
    finally:
        user_id_var.reset(token_u)
        tenant_id_var.reset(token_t)
    rendered = formatter.format(record)
    assert "tenant_id=t1" in rendered
    assert "user_id=u1" in rendered
