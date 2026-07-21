"""Every upstream call must be attributable: which prompt (name@version),
which request (correlation id). This is the runtime half of prompt version
management — git knows the history, the log knows what was live."""

import logging

import pytest

from azgenai_lab.core.correlation import correlation_id_var
from azgenai_lab.prompts.loader import PromptTemplate
from azgenai_lab.services.azure_openai import FakeChatService

PROMPT = PromptTemplate(name="default_chat", version=1, description="d", text="You are T.")


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
