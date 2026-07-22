"""Every upstream call must be attributable: which prompt (name@version),
which request (correlation id). This is the runtime half of prompt version
management — git knows the history, the log knows what was live."""

import hashlib
import io
import logging

import pytest

from azgenai_lab.core.correlation import correlation_id_var
from azgenai_lab.prompts.loader import PromptTemplate
from azgenai_lab.services.azure_openai import FakeChatService

_TEXT = "You are T."
PROMPT = PromptTemplate(
    name="default_chat",
    version=1,
    description="d",
    text=_TEXT,
    sha256=hashlib.sha256(_TEXT.encode("utf-8")).hexdigest(),
)

# Same format string as configure_logging() — extras aren't rendered by it, so
# the fields the article/incident responders grep for must be in the message
# itself, not only in the record's extra attributes.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


async def test_complete_logs_prompt_identity(caplog: pytest.LogCaptureFixture) -> None:
    token = correlation_id_var.set("cid-123")
    try:
        with caplog.at_level(logging.INFO, logger="azgenai_lab.services.azure_openai"):
            await FakeChatService(prompt=PROMPT).complete([{"role": "user", "content": "ping"}])
    finally:
        correlation_id_var.reset(token)
    record = next(r for r in caplog.records if getattr(r, "prompt_name", None))
    assert record.prompt_name == "default_chat"
    assert record.prompt_version == 1
    assert record.correlation_id == "cid-123"


async def test_open_stream_logs_prompt_identity(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="azgenai_lab.services.azure_openai"):
        await FakeChatService(prompt=PROMPT).open_stream([{"role": "user", "content": "ping"}])
    record = next(r for r in caplog.records if getattr(r, "prompt_name", None))
    assert record.prompt_name == "default_chat"
    assert record.correlation_id is None


async def test_complete_renders_prompt_identity_in_log_line() -> None:
    # Reproduces what configure_logging() actually renders — extras alone
    # don't survive the formatter, so this pins the fields into the message.
    logger = logging.getLogger("azgenai_lab.services.azure_openai")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    token = correlation_id_var.set("cid-123")
    try:
        await FakeChatService(prompt=PROMPT).complete([{"role": "user", "content": "ping"}])
    finally:
        correlation_id_var.reset(token)
        logger.removeHandler(handler)

    line = stream.getvalue()
    assert "prompt_name=default_chat" in line
    assert "prompt_version=1" in line
    assert f"prompt_sha256={PROMPT.sha256[:12]}" in line
    assert "cid-123" in line
