"""Day 27: the semantic span over a model call, and what may not be on it.

Without this span the dependency half of a request's lifecycle is empty: the
distro instruments neither the openai SDK nor httpx, so `/chat` would show a
server span and nothing underneath it.
"""

import pytest
from fastapi.testclient import TestClient
from tests.unit.telemetry_helpers import attribute_names, children_named, span_tree

from azgenai_lab.api.chat import get_conversation_service
from azgenai_lab.core.errors import InvalidInputError, UpstreamServiceError
from azgenai_lab.core.telemetry import ATTR_TTFB, FAKE_DEPLOYMENT

_SENTINEL = "SENTINEL-UPSTREAM-DETAIL"

# Substrings that would mean content leaked into telemetry. Day 15/19/21/22
# built these rules up one at a time; this is the recursive guard that keeps
# them from being re-litigated per attribute.
_FORBIDDEN_SUBSTRINGS = (
    "message",
    "content",
    "prompt_text",
    "question",
    "answer",
    "chunk",
    "group",
    "token_value",
    "claim",
    "argument",
    "detail",
)


class _RaisingChatService:
    """Substitutes only the LLM boundary; the orchestrator stays real.

    Wrapped in TracingChatService by the fixture below, exactly as the
    composition point wraps the real and fake adapters -- which is the point of
    putting the span on the protocol rather than inside an adapter.
    """

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def complete(self, items: object) -> object:
        raise self._error

    async def open_stream(self, items: object) -> object:
        raise self._error

    async def aclose(self) -> None:
        return None


@pytest.fixture
def failing_llm(telemetry_app):
    """Fail the LLM boundary only, through the same decorator the composition
    point applies. Following tests/unit/test_chat_api.py's override shape: the
    orchestrator stays real, and the app's own audit attribution is reused
    because the provider boundary was still reached."""
    from azgenai_lab.services.azure_openai import TracingChatService
    from azgenai_lab.services.conversation import ConversationChatService
    from azgenai_lab.services.conversation_store import InMemoryConversationStore

    def _install(error: Exception) -> None:
        service = ConversationChatService(
            TracingChatService(_RaisingChatService(error), FAKE_DEPLOYMENT),  # type: ignore[arg-type]
            InMemoryConversationStore(),
            audit_attribution=telemetry_app.state.conversation_service.audit_attribution,
        )
        telemetry_app.dependency_overrides[get_conversation_service] = lambda: service

    return _install


def test_chat_span_tree(telemetry_client: TestClient, span_exporter) -> None:
    assert telemetry_client.post("/api/v1/chat", json={"message": "hi"}).status_code == 200

    nodes = span_tree(span_exporter)
    llm = children_named(nodes, "POST ", "chat ")
    assert llm == [f"chat {FAKE_DEPLOYMENT}"]


def test_llm_span_carries_gen_ai_attributes(telemetry_client: TestClient, span_exporter) -> None:
    telemetry_client.post("/api/v1/chat", json={"message": "hi"})

    llm = next(s for s in span_exporter.get_finished_spans() if s.name.startswith("chat "))
    attrs = llm.attributes or {}
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.provider.name"] == "azure.ai.openai"
    assert attrs["gen_ai.request.model"] == FAKE_DEPLOYMENT
    assert attrs["gen_ai.usage.input_tokens"] > 0
    assert attrs["gen_ai.usage.output_tokens"] > 0
    assert attrs["gen_ai.response.finish_reasons"] == ("completed",)
    assert attrs["azgenai.outcome"] == "success"
    # Absent, not "" and not null: a successful call has no error code, and the
    # honest way to say so is for the key not to exist.
    assert "azgenai.error.code" not in attrs
    # Non-streaming: there is no first chunk to time.
    assert ATTR_TTFB not in attrs


@pytest.mark.parametrize(
    "error,expected_outcome,expected_code,expected_status",
    [
        (InvalidInputError(_SENTINEL), "rejected", "invalid_input", 400),
        (UpstreamServiceError(_SENTINEL), "error", "upstream_error", 502),
    ],
)
def test_outcome_follows_upstream_outcome(
    telemetry_client: TestClient,
    span_exporter,
    failing_llm,
    error: Exception,
    expected_outcome: str,
    expected_code: str,
    expected_status: int,
) -> None:
    # 4xx is this caller's request being rejected; 5xx is a failure that was
    # not their fault. core/errors.py already draws that line for the audit
    # log, and the span reuses it rather than deciding again.
    failing_llm(error)

    response = telemetry_client.post("/api/v1/chat", json={"message": "hi"})
    assert response.status_code == expected_status

    llm = next(s for s in span_exporter.get_finished_spans() if s.name.startswith("chat "))
    attrs = llm.attributes or {}
    assert attrs["azgenai.outcome"] == expected_outcome
    assert attrs["azgenai.error.code"] == expected_code
    # Usage is unknown on a failed call, so the keys stay absent rather than
    # reporting zero -- the upstream may still have been billed (Day 9).
    assert "gen_ai.usage.output_tokens" not in attrs


def test_upstream_detail_never_reaches_the_span(
    telemetry_client: TestClient, span_exporter, failing_llm
) -> None:
    # record_exception defaults to True and writes the exception's message into
    # a span event verbatim. The sentinel is what proves the flag is actually
    # off, rather than the message merely being uninteresting.
    failing_llm(UpstreamServiceError(_SENTINEL))
    telemetry_client.post("/api/v1/chat", json={"message": "hi"})

    for span in span_exporter.get_finished_spans():
        blob = repr((span.attributes, [(e.name, e.attributes) for e in span.events]))
        assert _SENTINEL not in blob
        assert [e for e in span.events if e.name == "exception"] == []


def test_error_span_status_is_set_without_a_message(
    telemetry_client: TestClient, span_exporter, failing_llm
) -> None:
    failing_llm(UpstreamServiceError(_SENTINEL))
    telemetry_client.post("/api/v1/chat", json={"message": "hi"})

    llm = next(s for s in span_exporter.get_finished_spans() if s.name.startswith("chat "))
    assert llm.status.status_code.name == "ERROR"
    # The classification, never the upstream's own words.
    assert llm.status.description == "upstream_error"


def test_no_forbidden_attribute_names_anywhere(
    telemetry_client: TestClient, span_exporter
) -> None:
    telemetry_client.post("/api/v1/chat", json={"message": "hi"})

    for name in attribute_names(span_exporter):
        lowered = name.lower()
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            assert forbidden not in lowered, f"{name} looks like it carries content"
