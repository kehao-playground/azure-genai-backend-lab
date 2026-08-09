import json
from datetime import datetime, timedelta, timezone
from typing import get_args

import pytest
from pydantic import ValidationError
from tests.unit.audit_helpers import NOW
from tests.unit.audit_helpers import base_fields as _base
from tests.unit.audit_helpers import unattempted_fields as _unattempted

from azgenai_lab.core import audit
from azgenai_lab.core.audit import (
    AUDIT_EVENT_ADAPTER,
    AgentRunErrorEvent,
    AuditTool,
    AuditUsage,
    AuthRejected,
    ChatTurnError,
    ChatTurnRejected,
    ChatTurnSuccess,
    RagQueryError,
    RagQuerySuccess,
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


_ENVELOPE = {"schema_version", "occurred_at", "correlation_id", "duration_ms"}
_ROUTE = _ENVELOPE | {"tenant_id", "user_id", "provider_call_attempted",
                      "prompt_name", "prompt_version", "prompt_sha256", "deployment", "usage"}
_CHAT = _ROUTE | {"event", "outcome", "conversation_id", "streaming", "committed",
                  "model_version", "status", "incomplete_reason"}
_RAG = _ROUTE | {"event", "outcome", "model_version", "hit_count",
                 "selected_chunk_ids", "status", "failed_stage"}
_AGENT = _ROUTE | {"event", "outcome", "conversation_id", "committed", "model_calls",
                   "tool_call_count", "refused_call_count", "tools", "stop_reason"}

EXPECTED_FIELDS = {
    "ChatTurnSuccess": _CHAT,
    "ChatTurnRejected": _CHAT | {"error_code", "spent", "budget"},
    "ChatTurnError": _CHAT | {"error_code"},
    "RagQuerySuccess": _RAG,
    "RagQueryRejected": _RAG | {"error_code"},
    "RagQueryError": _RAG | {"error_code"},
    "AgentRunSuccess": _AGENT,
    "AgentRunRejected": _AGENT | {"error_code", "spent", "budget"},
    "AgentRunErrorEvent": _AGENT | {"error_code"},
    "AuthRejected": _ENVELOPE | {"event", "outcome", "tenant_id", "user_id",
                                 "path", "auth_mode", "reason", "http_status"},
}


def test_variant_field_sets_are_exact():
    for name, expected in EXPECTED_FIELDS.items():
        cls = getattr(audit, name)
        assert set(cls.model_fields) == expected, name


# The approved never-log list, spelled out as the exact compound names an
# implementer would plausibly reach for. Exact equality — substring matching
# would ban legitimate names like total_tokens.
FORBIDDEN_FIELD_NAMES = {
    "message", "message_text", "question", "question_text", "content",
    "chunk_content", "chunk_text", "text", "answer", "task", "arguments",
    "tool_arguments", "detail", "upstream_detail", "claims", "token",
    "raw_token", "access_token", "bearer_token", "snippet", "body",
    "exception_message", "validation_error_message", "group_ids", "groups",
}


def _nested_models(annotation) -> list[type]:
    """Unwrap Optional/tuple/Annotated/... and return contained BaseModel types."""
    from pydantic import BaseModel as _BM
    found: list[type] = []
    stack = [annotation]
    while stack:
        current = stack.pop()
        if isinstance(current, type):
            if issubclass(current, _BM):
                found.append(current)
        else:
            stack.extend(get_args(current))
    return found


def test_no_content_bearing_field_anywhere():
    seen: set[type] = set()

    def walk(model_cls: type) -> None:
        if model_cls in seen:
            return
        seen.add(model_cls)
        for field_name, field in model_cls.model_fields.items():
            assert field_name not in FORBIDDEN_FIELD_NAMES, (model_cls.__name__, field_name)
            for sub in _nested_models(field.annotation):
                walk(sub)

    for name in EXPECTED_FIELDS:
        walk(getattr(audit, name))


def _rag_base(**overrides):
    fields = dict(
        occurred_at=NOW, correlation_id="cid-1", duration_ms=20.0,
        tenant_id="t1", user_id="u1", provider_call_attempted=True,
        prompt_name="rag_answer", prompt_version=3, prompt_sha256="cd" * 32,
        deployment="fake", model_version="fake",
        usage=AuditUsage(input_tokens=100, output_tokens=20, total_tokens=120),
        hit_count=3, selected_chunk_ids=("c-1", "c-2"), status="answered",
    )
    fields.update(overrides)
    return fields


def _rag_unattempted(**overrides):
    fields = _rag_base(provider_call_attempted=False, prompt_name=None, prompt_version=None,
                       prompt_sha256=None, deployment=None, model_version=None, usage=None)
    fields.update(overrides)
    return fields


def test_rag_answered_requires_attempted_no_answer_forbids_it():
    ok = RagQuerySuccess(**_rag_unattempted(status="no_answer", hit_count=0,
                                            selected_chunk_ids=None))
    assert ok.status == "no_answer"
    with pytest.raises(ValidationError):
        RagQuerySuccess(**_rag_unattempted())            # answered without attempt
    with pytest.raises(ValidationError):
        RagQuerySuccess(**_rag_base(status="no_answer"))  # no_answer with attempt


def test_rag_error_requires_failed_stage():
    with pytest.raises(ValidationError):
        RagQueryError(**_rag_unattempted(status="error", error_code="embedding_rejected",
                                         hit_count=None, selected_chunk_ids=None,
                                         failed_stage=None))
    ok = RagQueryError(**_rag_unattempted(status="error", error_code="embedding_rejected",
                                          hit_count=None, selected_chunk_ids=None,
                                          failed_stage="retrieve"))
    assert ok.failed_stage == "retrieve"


def _agent_fields(**overrides):
    fields = dict(
        occurred_at=NOW, correlation_id="cid-1", duration_ms=90.0,
        tenant_id="t1", user_id="u1", conversation_id="c9", committed=False,
        provider_call_attempted=True, prompt_name="ops_agent", prompt_version=2,
        prompt_sha256="ef" * 32, deployment="fake", usage=None,
        model_calls=None, tool_call_count=None, refused_call_count=None,
        tools=(AuditTool(name="search_docs", executed=True, round_index=None),),
        stop_reason=None,
    )
    fields.update(overrides)
    return fields


def test_agent_degraded_counts_are_null_not_zero():
    event = AgentRunErrorEvent(**_agent_fields(error_code="upstream_error"))
    assert event.model_calls is None and event.tools[0].round_index is None


def _auth(**overrides):
    fields = dict(occurred_at=NOW, correlation_id="c", duration_ms=1.0,
                  tenant_id=None, user_id=None, path="/api/v1/chat",
                  auth_mode="entra", reason="token_invalid", http_status=401)
    fields.update(overrides)
    return fields


def test_auth_rejected_identity_rules():
    assert AuthRejected(**_auth()).outcome == "rejected"
    with pytest.raises(ValidationError):    # 401 with identity
        AuthRejected(**_auth(tenant_id="t1", user_id="u1"))
    with pytest.raises(ValidationError):    # 403 without identity
        AuthRejected(**_auth(reason="permission_missing", http_status=403))
    ok_403 = AuthRejected(**_auth(tenant_id="t1", user_id="u1",
                                  reason="permission_missing", http_status=403))
    assert ok_403.http_status == 403
    with pytest.raises(ValidationError):    # permission_missing is 403-only
        AuthRejected(**_auth(reason="permission_missing"))


def test_auth_mode_reason_binding():
    with pytest.raises(ValidationError):    # headers mode never yields 403
        AuthRejected(**_auth(auth_mode="headers", tenant_id="t1", user_id="u1",
                             reason="permission_missing", http_status=403))
    with pytest.raises(ValidationError):    # headers mode never uses bearer reasons
        AuthRejected(**_auth(auth_mode="headers", reason="bearer_missing"))
    with pytest.raises(ValidationError):    # entra 401 only bearer_missing|token_invalid
        AuthRejected(**_auth(reason="headers_missing"))
    ok = AuthRejected(**_auth(auth_mode="headers", reason="headers_missing"))
    assert ok.auth_mode == "headers"


def test_union_round_trip_by_discriminators():
    event = ChatTurnSuccess(**_base())
    parsed = AUDIT_EVENT_ADAPTER.validate_python(event.model_dump(mode="json"))
    assert isinstance(parsed, ChatTurnSuccess)


# --- Exported schema (Task 11): structural + per-$def constraint assertions ---
#
# text.count(...) below counts each inner discriminated union twice: pydantic
# inlines a nested discriminated union's full schema both under the outer
# discriminator's mapping value and again inside the outer oneOf array (no
# $ref sharing between the two), so three inner "outcome" unions -> six
# occurrences of that propertyName, not three. Verified directly against the
# exported schema, not assumed from the brief's illustrative count.


def test_exported_schema_structure_and_constraints():
    schema = AUDIT_EVENT_ADAPTER.json_schema()
    text = json.dumps(schema)
    assert '"propertyName": "event"' in text            # outer discriminator
    assert text.count('"propertyName": "outcome"') == 6  # three inner unions, inlined twice each
    defs = schema["$defs"]

    def def_text(name: str) -> str:
        return json.dumps(defs[name])

    assert audit.CHAT_SUCCESS_CONSTRAINT in def_text("ChatTurnSuccess")
    assert audit.AGENT_SUCCESS_CONSTRAINT in def_text("AgentRunSuccess")
    assert audit.RAG_SUCCESS_CONSTRAINT in def_text("RagQuerySuccess")
    assert audit.BUDGET_CONSTRAINT in def_text("ChatTurnRejected")
    assert audit.BUDGET_CONSTRAINT in def_text("AgentRunRejected")
    assert audit.IDENTITY_CONSTRAINT in def_text("AuthRejected")
    for name in ("ChatTurnSuccess", "RagQueryError", "AgentRunRejected"):
        assert audit.ATTEMPTED_CONSTRAINT in def_text(name)


def test_outer_union_covers_all_four_event_families():
    """A mis-wired branch (e.g. rag.query left out of AuditEvent) would not
    surface from test_union_round_trip_by_discriminators alone, since that
    test only round-trips chat.turn. Assert the outer discriminator's
    mapping directly instead of only asserting it exists."""
    schema = AUDIT_EVENT_ADAPTER.json_schema()
    assert set(schema["discriminator"]["mapping"]) == {
        "chat.turn", "rag.query", "agent.run", "auth.rejected",
    }
