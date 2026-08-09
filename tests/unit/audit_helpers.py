"""Shared fixtures/helpers for the Day 22 audit test files."""

import json
from datetime import UTC, datetime

import pytest

from azgenai_lab.core.audit import AuditUsage

IDENTITY = {"X-Tenant-Id": "t1", "X-User-Id": "u1"}
NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)


def audit_events(caplog: "pytest.LogCaptureFixture") -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "audit"]


def base_fields(**overrides) -> dict:
    fields = dict(
        occurred_at=NOW, correlation_id="cid-1", duration_ms=12.5,
        tenant_id="t1", user_id="u1", conversation_id="c1", streaming=False,
        committed=True, provider_call_attempted=True,
        prompt_name="default_chat", prompt_version=1, prompt_sha256="ab" * 32,
        deployment="fake", model_version="fake",
        usage=AuditUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        status="completed",
    )
    fields.update(overrides)
    return fields


def unattempted_fields(**overrides) -> dict:
    fields = base_fields(
        provider_call_attempted=False, prompt_name=None, prompt_version=None,
        prompt_sha256=None, deployment=None, model_version=None, usage=None,
        status=None, committed=False,
    )
    fields.update(overrides)
    return fields
