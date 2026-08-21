"""Day 27: the RAG pipeline's span tree, including who is whose child.

The parentage is the part a flat list of span names cannot check, and the part
a reader of a trace actually needs: "embeddings took 300ms" means something
different depending on whether it sits under retrieval or beside it.
"""

import pytest
from fastapi.testclient import TestClient
from tests.unit.telemetry_helpers import attribute_names, children_named, span_tree

from azgenai_lab.api.rag import get_rag_service
from azgenai_lab.core.telemetry import FAKE_DEPLOYMENT, FAKE_EMBEDDING_DEPLOYMENT
from azgenai_lab.services.azure_openai import TracingChatService
from azgenai_lab.services.azure_search import FakeSearchClient
from azgenai_lab.services.embeddings import FakeEmbeddingClient
from azgenai_lab.services.rag import RagService
from azgenai_lab.services.retrieval import Retriever

_STAGES = {"rag.retrieval", "rag.assemble_context", "rag.generation"}
# The question the BDD suite uses because the fake corpus actually matches it
# (tests/bdd/steps/rag_steps.py). A question with no hits takes Day 14's
# structural no-answer path, which is a different tree -- asserted separately
# below rather than accidentally.
_ANSWERABLE = "What does hybrid search combine?"
_CORPUS = [
    {
        "chunk_id": "chunk-1",
        "parent_id": "doc-1",
        "title": "Hybrid Search Overview",
        "heading_path": "Hybrid Search Overview > Basics",
        "content": "Hybrid search combines vector and keyword search results.",
        "tenant_id": "t1",
        "allowed_groups": [],
    }
]


class _StubChatService:
    """Fixed cited answer, input-independent -- the same stand-in shape
    tests/bdd/steps/rag_steps.py uses, wrapped in the tracing decorator so the
    generation stage produces the span the composition point would give it."""

    async def complete(self, items: object) -> object:
        from azgenai_lab.services.azure_openai import ChatResult

        return ChatResult(message="Alpha pairs with beta [1].", model_version="stub")

    async def aclose(self) -> None:
        return None


@pytest.fixture
def seeded_corpus(telemetry_app):
    """Give the fake index something to match.

    Without this the retriever returns zero hits and every request takes Day
    14's structural no-answer path -- a different, shorter tree. The parentage
    test needs the full one, so the corpus is explicit rather than incidental.
    """

    def _install(documents=_CORPUS) -> None:
        service = RagService(
            Retriever(FakeEmbeddingClient(), FakeSearchClient(documents), top=5),
            TracingChatService(_StubChatService(), FAKE_DEPLOYMENT),  # type: ignore[arg-type]
            audit_attribution=telemetry_app.state.rag_service.audit_attribution,
        )
        telemetry_app.dependency_overrides[get_rag_service] = lambda: service

    return _install


def test_rag_span_tree_parentage(
    telemetry_client: TestClient, span_exporter, seeded_corpus
) -> None:
    seeded_corpus()
    assert telemetry_client.post("/api/v1/rag", json={"question": _ANSWERABLE}).status_code == 200

    nodes = span_tree(span_exporter)
    assert set(children_named(nodes, "POST ", "rag.")) == _STAGES
    # The assertion a flat name list would miss: these two hang off retrieval,
    # not off the server span.
    assert set(children_named(nodes, "rag.retrieval", "")) == {
        f"embeddings {FAKE_EMBEDDING_DEPLOYMENT}",
        "azure.search.query",
    }
    assert children_named(nodes, "rag.generation", "chat ") == [f"chat {FAKE_DEPLOYMENT}"]
    assert children_named(nodes, "rag.assemble_context", "") == []


def test_no_answer_path_has_no_generation_stage(
    telemetry_client: TestClient, span_exporter, seeded_corpus
) -> None:
    # Day 14's structural no-answer: zero hits means the model is never called
    # at all, and the span tree should show exactly that rather than an empty
    # generation span suggesting a call that cost nothing.
    # An empty corpus, not an unrelated question relying on lexical
    # non-matching: the contract is structural, so the fixture should be too.
    seeded_corpus([])
    body = telemetry_client.post("/api/v1/rag", json={"question": _ANSWERABLE}).json()
    assert body["status"] == "no_answer"

    nodes = span_tree(span_exporter)
    stages = set(children_named(nodes, "POST ", "rag."))
    assert "rag.generation" not in stages
    assert [n for n in nodes.values() if n.name.startswith("chat ")] == []


def test_search_span_carries_hit_count_and_no_acl_detail(
    telemetry_client: TestClient, span_exporter, seeded_corpus
) -> None:
    seeded_corpus()
    telemetry_client.post("/api/v1/rag", json={"question": _ANSWERABLE})

    search = next(
        span for span in span_exporter.get_finished_spans() if span.name == "azure.search.query"
    )
    attrs = search.attributes or {}
    assert attrs["azgenai.search.hit_count"] >= 0
    # Day 15's ACL filter is built from group ids. None of it goes near a span.
    assert [key for key in attrs if "group" in key.lower() or "filter" in key.lower()] == []


def test_rag_spans_carry_the_correlation_id(
    telemetry_client: TestClient, span_exporter, seeded_corpus
) -> None:
    seeded_corpus()
    telemetry_client.post(
        "/api/v1/rag",
        json={"question": _ANSWERABLE},
        headers={"X-Correlation-Id": "rag-case"},
    )

    stage_spans = [
        span for span in span_exporter.get_finished_spans() if span.name in _STAGES
    ]
    assert stage_spans != []
    for span in stage_spans:
        # Application-owned spans carry the exact id; the httpx dependency
        # spans below them rely on the trace tree instead.
        assert (span.attributes or {})["correlation_id"] == "rag-case"


def test_no_forbidden_attribute_names_on_the_rag_path(
    telemetry_client: TestClient, span_exporter, seeded_corpus
) -> None:
    seeded_corpus()
    telemetry_client.post("/api/v1/rag", json={"question": _ANSWERABLE})

    forbidden = ("question", "content", "chunk", "group", "answer", "message")
    for name in attribute_names(span_exporter):
        lowered = name.lower()
        for needle in forbidden:
            assert needle not in lowered, f"{name} looks like it carries content"
