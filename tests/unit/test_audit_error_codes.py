"""Observed bidirectional trace of the spec §1c error-code sets.

Every table row drives a real emission path and asserts the event; the
completeness tests prove the tables cover the Literal sets exactly. A row
that cannot be driven means the §1c table is wrong: STOP and report back to
the spec — do not edit the Literal here.

Each ``test_*_case`` below is a per-case parametrized test (not a
module-level accumulator) so no test's result depends on another test having
run first: every case gets its own fresh ``client`` fixture and its own
``[event] = audit_events(caplog)`` assertion. The drivers reuse the exact
fixture factories Task 6-10 built (now shared via ``tests/unit/audit_helpers
.py`` so this file and the owning test files drive the identical doubles,
not a second copy of them).
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import get_args

import pytest
from fastapi.testclient import TestClient
from tests.unit.audit_helpers import (
    IDENTITY,
    audit_events,
    with_broken_agent_append_store,
    with_broken_chat_append_store,
    with_broken_generation,
    with_oversized_hit,
    with_raising_agent_service,
    with_raising_chat_service,
    with_raising_retriever,
    with_tiny_agent_budget,
    with_tiny_chat_budget,
)

from azgenai_lab.api.streaming import _audit_observed
from azgenai_lab.core.audit import (
    AgentAuditTerminalSnapshot,
    AgentErrorCode,
    AgentRejectedCode,
    AuditAttribution,
    AuditToolExecution,
    ChatErrorCode,
    ChatRejectedCode,
    RagErrorCode,
    RagRejectedCode,
    chat_base_fields,
)
from azgenai_lab.core.errors import (
    ConfigurationError,
    ContentFilteredError,
    InvalidInputError,
    UpstreamServiceError,
    UpstreamThrottledError,
    UpstreamTimeoutError,
)
from azgenai_lab.services.agent_framework import AgentRunError
from azgenai_lab.services.azure_openai import TextDelta
from azgenai_lab.services.azure_search import SearchRequestRejectedError, SearchUnavailableError
from azgenai_lab.services.embeddings import EmbeddingRejectedError


@dataclass(frozen=True)
class Case:
    code: str
    outcome: str
    driver: str          # dispatch key below
    exc: Exception | None = None


CHAT_CASES = [
    Case("content_filtered", "rejected", "raise", ContentFilteredError("x")),
    Case("invalid_input", "rejected", "raise", InvalidInputError("x")),
    Case("configuration_error", "error", "raise", ConfigurationError("x")),
    Case("upstream_throttled", "error", "raise", UpstreamThrottledError("x")),
    Case("upstream_timeout", "error", "raise", UpstreamTimeoutError("x")),
    Case("upstream_error", "error", "raise", UpstreamServiceError("x")),
    Case("conversation_not_found", "rejected", "unknown_conversation"),
    Case("token_budget_exceeded", "rejected", "tiny_budget"),
    Case("validation_error", "rejected", "bad_body"),
    Case("storage_error", "error", "broken_append"),
    Case("client_disconnect", "error", "generator_disconnect"),
]

RAG_CASES = [
    Case("content_filtered", "rejected", "generation_raise", ContentFilteredError("x")),
    Case("validation_error", "rejected", "bad_body"),
    Case("configuration_error", "error", "generation_raise", ConfigurationError("x")),
    # EmbeddingRejectedError is built in the driver, not carried as .exc.
    Case("embedding_rejected", "error", "retriever_raise"),
    Case("rag_context_overflow", "error", "oversized_hit"),
    Case("search_unavailable", "error", "retriever_raise_search_unavailable"),
    Case("search_request_rejected", "error", "retriever_raise_search_rejected"),
    Case("upstream_throttled", "error", "generation_raise", UpstreamThrottledError("x")),
    Case("upstream_timeout", "error", "generation_raise", UpstreamTimeoutError("x")),
    Case("upstream_error", "error", "generation_raise", UpstreamServiceError("x")),
]

AGENT_CASES = [
    Case("validation_error", "rejected", "bad_body"),
    Case("invalid_input", "rejected", "oversized_task"),
    Case("conversation_not_found", "rejected", "unknown_conversation"),
    Case("token_budget_exceeded", "rejected", "tiny_budget"),
    Case("storage_error", "error", "broken_append"),
    Case("upstream_error", "error", "agent_run_error"),
]


# --- Driver dispatch ---------------------------------------------------


async def _drive_chat_disconnect(caplog: pytest.LogCaptureFixture) -> None:
    """Task 7's post-transfer observer generator, driven directly (not
    through TestClient) -- same as test_audit_streaming.py's own disconnect
    tests, and for the same reason: an endpoint-level TestClient disconnect
    cannot pin the exact point of disconnect the way calling ``aclose()`` by
    hand can."""

    async def deltas_forever():
        yield TextDelta("a")
        await asyncio.sleep(3600)

    gen = _audit_observed(
        deltas_forever(),
        base=chat_base_fields(
            tenant_id="t1", user_id="u1", correlation_id="cid-1",
            conversation_id="c1", streaming=True,
        ),
        attribution=AuditAttribution("default_chat", 1, "ab" * 32, "fake"),
        audit_start=0.0,
    )
    with caplog.at_level(logging.INFO, logger="audit"):
        assert isinstance(await anext(gen), TextDelta)
        await gen.aclose()


async def _drive_chat(case: Case, client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    if case.driver == "raise":
        with_raising_chat_service(client, case.exc)
        with caplog.at_level(logging.INFO, logger="audit"):
            client.post("/api/v1/chat", json={"message": "hi"}, headers=IDENTITY)
    elif case.driver == "unknown_conversation":
        with caplog.at_level(logging.INFO, logger="audit"):
            client.post(
                "/api/v1/chat",
                json={"message": "hi", "conversation_id": "ghost"},
                headers=IDENTITY,
            )
    elif case.driver == "tiny_budget":
        with_tiny_chat_budget(client)
        first = client.post("/api/v1/chat", json={"message": "hi"}, headers=IDENTITY)
        cid = first.json()["conversation_id"]
        caplog.clear()  # discard the first (budget-setup) turn's own event
        with caplog.at_level(logging.INFO, logger="audit"):
            client.post(
                "/api/v1/chat",
                json={"message": "again", "conversation_id": cid},
                headers=IDENTITY,
            )
    elif case.driver == "bad_body":
        with caplog.at_level(logging.INFO, logger="audit"):
            client.post("/api/v1/chat", json={"message": ""}, headers=IDENTITY)
    elif case.driver == "broken_append":
        with_broken_chat_append_store(client)
        with caplog.at_level(logging.INFO, logger="audit"):
            client.post("/api/v1/chat", json={"message": "hi"}, headers=IDENTITY)
    elif case.driver == "generator_disconnect":
        await _drive_chat_disconnect(caplog)
    else:
        raise AssertionError(f"unhandled chat driver {case.driver!r}")


async def _drive_rag(case: Case, client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    if case.driver == "generation_raise":
        with_broken_generation(client, case.exc)
        with caplog.at_level(logging.INFO, logger="audit"):
            client.post(
                "/api/v1/rag", json={"question": "refund window?"}, headers=IDENTITY
            )
    elif case.driver == "bad_body":
        with caplog.at_level(logging.INFO, logger="audit"):
            client.post("/api/v1/rag", json={"question": "   "}, headers=IDENTITY)
    elif case.driver == "retriever_raise":
        with_raising_retriever(client, EmbeddingRejectedError("bad input"))
        with caplog.at_level(logging.INFO, logger="audit"):
            client.post("/api/v1/rag", json={"question": "x"}, headers=IDENTITY)
    elif case.driver == "oversized_hit":
        with_oversized_hit(client)
        with caplog.at_level(logging.INFO, logger="audit"):
            client.post("/api/v1/rag", json={"question": "x"}, headers=IDENTITY)
    elif case.driver == "retriever_raise_search_unavailable":
        with_raising_retriever(client, SearchUnavailableError("search down"))
        with caplog.at_level(logging.INFO, logger="audit"):
            client.post("/api/v1/rag", json={"question": "x"}, headers=IDENTITY)
    elif case.driver == "retriever_raise_search_rejected":
        with_raising_retriever(client, SearchRequestRejectedError("bad request"))
        with caplog.at_level(logging.INFO, logger="audit"):
            client.post("/api/v1/rag", json={"question": "x"}, headers=IDENTITY)
    else:
        raise AssertionError(f"unhandled rag driver {case.driver!r}")


async def _drive_agent(case: Case, client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    if case.driver == "bad_body":
        with caplog.at_level(logging.INFO, logger="audit"):
            client.post("/api/v1/agent", json={"task": " "}, headers=IDENTITY)
    elif case.driver == "oversized_task":
        with caplog.at_level(logging.INFO, logger="audit"):
            client.post("/api/v1/agent", json={"task": "x" * 5000}, headers=IDENTITY)
    elif case.driver == "unknown_conversation":
        with caplog.at_level(logging.INFO, logger="audit"):
            client.post(
                "/api/v1/agent",
                json={"task": "hi", "conversation_id": "ghost"},
                headers=IDENTITY,
            )
    elif case.driver == "tiny_budget":
        with_tiny_agent_budget(client)
        first = client.post("/api/v1/agent", json={"task": "hi"}, headers=IDENTITY)
        cid = first.json()["conversation_id"]
        caplog.clear()  # discard the first (budget-setup) turn's own event
        with caplog.at_level(logging.INFO, logger="audit"):
            client.post(
                "/api/v1/agent",
                json={"task": "again", "conversation_id": cid},
                headers=IDENTITY,
            )
    elif case.driver == "broken_append":
        with_broken_agent_append_store(client)
        with caplog.at_level(logging.INFO, logger="audit"):
            client.post("/api/v1/agent", json={"task": "hi"}, headers=IDENTITY)
    elif case.driver == "agent_run_error":
        # Same degraded-snapshot shape as test_audit_agent.py's
        # degraded_agent_client fixture: one tool ran before the
        # framework/provider boundary failed, every framework-derived field
        # honestly None.
        snapshot = AgentAuditTerminalSnapshot(
            provider_call_attempted=True,
            executions=(AuditToolExecution(name="search_docs", executed=True, round_index=None),),
            model_calls=None, tool_call_count=None, refused_call_count=None,
            stop_reason=None, usage=None,
        )
        error = AgentRunError("framework blew up", usage=None, audit_snapshot=snapshot)
        with_raising_agent_service(client, error)
        with caplog.at_level(logging.INFO, logger="audit"):
            client.post("/api/v1/agent", json={"task": "hi"}, headers=IDENTITY)
    else:
        raise AssertionError(f"unhandled agent driver {case.driver!r}")


# --- Per-case parametrized emission tests -------------------------------


@pytest.mark.parametrize("case", CHAT_CASES, ids=[c.code for c in CHAT_CASES])
async def test_chat_case(
    case: Case, client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    await _drive_chat(case, client, caplog)
    [event] = audit_events(caplog)
    assert (event["error_code"], event["outcome"]) == (case.code, case.outcome)


@pytest.mark.parametrize("case", RAG_CASES, ids=[c.code for c in RAG_CASES])
async def test_rag_case(
    case: Case, client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    await _drive_rag(case, client, caplog)
    [event] = audit_events(caplog)
    assert (event["error_code"], event["outcome"]) == (case.code, case.outcome)


@pytest.mark.parametrize("case", AGENT_CASES, ids=[c.code for c in AGENT_CASES])
async def test_agent_case(
    case: Case, client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    await _drive_agent(case, client, caplog)
    [event] = audit_events(caplog)
    assert (event["error_code"], event["outcome"]) == (case.code, case.outcome)


# --- Case-table completeness (no global, no order dependence) ----------


def test_chat_case_table_complete():
    assert {c.code for c in CHAT_CASES} == \
        set(get_args(ChatRejectedCode)) | set(get_args(ChatErrorCode))


def test_rag_case_table_complete():
    assert {c.code for c in RAG_CASES} == \
        set(get_args(RagRejectedCode)) | set(get_args(RagErrorCode))


def test_agent_case_table_complete():
    assert {c.code for c in AGENT_CASES} == \
        set(get_args(AgentRejectedCode)) | set(get_args(AgentErrorCode))
