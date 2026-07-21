"""Startup must actually configure logging: `configure_logging` existing but
uncalled means every INFO log — including the per-LLM-call prompt-identity
line — is silently dropped under a plain `uvicorn` run (no pytest logging
capture, no basicConfig from elsewhere)."""

import logging

from azgenai_lab.core.config import Settings
from azgenai_lab.core.logging import configure_logging


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
