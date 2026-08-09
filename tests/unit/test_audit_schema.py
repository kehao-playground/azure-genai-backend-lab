from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from tests.unit.audit_helpers import NOW
from tests.unit.audit_helpers import base_fields as _base
from tests.unit.audit_helpers import unattempted_fields as _unattempted

from azgenai_lab.core.audit import (
    AuditTool,
    AuditUsage,
    ChatTurnError,
    ChatTurnRejected,
    ChatTurnSuccess,
)


def test_success_event_constructs_and_serializes():
    dumped = ChatTurnSuccess(**_base()).model_dump(mode="json")
    assert dumped["event"] == "chat.turn" and dumped["outcome"] == "success"
    assert dumped["schema_version"] == 1
    assert "error_code" not in dumped and "spent" not in dumped


def test_naive_datetime_rejected():
    with pytest.raises(ValidationError):
        ChatTurnSuccess(**_base(occurred_at=datetime(2026, 8, 9, 12, 0)))


def test_non_utc_normalized():
    taipei = timezone(timedelta(hours=8))
    event = ChatTurnSuccess(**_base(occurred_at=NOW.astimezone(taipei)))
    assert event.occurred_at.utcoffset().total_seconds() == 0


def test_success_requires_attempted_true():
    with pytest.raises(ValidationError):
        ChatTurnSuccess(**_unattempted())


def test_attempted_true_requires_full_attribution():
    with pytest.raises(ValidationError):
        ChatTurnSuccess(**_base(deployment=None))


def test_attempted_false_forbids_terminal_data():
    with pytest.raises(ValidationError):
        ChatTurnRejected(**_unattempted(error_code="conversation_not_found", model_version="fake"))


def test_budget_pair_iff_token_budget_exceeded():
    ok = ChatTurnRejected(**_unattempted(error_code="token_budget_exceeded",
                                         spent=50_100, budget=50_000))
    assert ok.spent == 50_100
    with pytest.raises(ValidationError):
        ChatTurnRejected(**_unattempted(error_code="token_budget_exceeded"))
    with pytest.raises(ValidationError):
        ChatTurnRejected(**_unattempted(error_code="token_budget_exceeded", spent=1))
    with pytest.raises(ValidationError):
        ChatTurnRejected(**_unattempted(error_code="conversation_not_found", spent=1, budget=2))


def test_unknown_field_and_wrong_code_rejected():
    with pytest.raises(ValidationError):
        ChatTurnSuccess(**_base(error_code="upstream_error"))
    with pytest.raises(ValidationError):
        ChatTurnError(**_unattempted(error_code="search_unavailable"))


def test_deep_freeze():
    usage = AuditUsage(input_tokens=1, output_tokens=1, total_tokens=2)
    with pytest.raises(ValidationError):
        usage.input_tokens = 9
    tool = AuditTool(name="search_docs", executed=True, round_index=1)
    with pytest.raises(ValidationError):
        tool.executed = False
    with pytest.raises(ValidationError):
        AuditTool(name="x", executed=True, round_index=0)
