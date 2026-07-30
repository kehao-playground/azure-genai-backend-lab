from collections.abc import Sequence

from behave import given, then, when

from azgenai_lab.api.rag import get_rag_service
from azgenai_lab.main import app
from azgenai_lab.models.conversation import ReplayItem
from azgenai_lab.prompts.loader import load_prompt
from azgenai_lab.services.azure_openai import ChatResult, FakeChatService
from azgenai_lab.services.azure_search import FakeSearchClient
from azgenai_lab.services.embeddings import FakeEmbeddingClient
from azgenai_lab.services.rag import RagService
from azgenai_lab.services.retrieval import Retriever

_QUESTION = "What does hybrid search combine?"


class CountingChatService:
    """Wraps FakeChatService and records how many times it was called."""

    def __init__(self) -> None:
        self._delegate = FakeChatService(prompt=load_prompt("rag_answer"))
        self.call_count = 0

    async def complete(self, items: Sequence[ReplayItem]) -> ChatResult:
        self.call_count += 1
        return await self._delegate.complete(items)


def _override_rag_service(documents: Sequence[dict[str, str]], context) -> None:  # type: ignore[no-untyped-def]
    search_client = FakeSearchClient(documents)
    retriever = Retriever(FakeEmbeddingClient(), search_client, top=5)
    chat = CountingChatService()
    context.rag_chat_spy = chat
    service = RagService(retriever, chat)  # type: ignore[arg-type]
    app.dependency_overrides[get_rag_service] = lambda: service


@given("an indexed corpus that covers the question")
def step_corpus_covers_question(context) -> None:  # type: ignore[no-untyped-def]
    context.question = _QUESTION
    documents = [
        {
            "chunk_id": "chunk-1",
            "parent_id": "doc-1",
            "title": "Hybrid Search Overview",
            "heading_path": "Hybrid Search Overview > Basics",
            "content": "Hybrid search combines vector and keyword search results.",
        }
    ]
    _override_rag_service(documents, context)


@given("an indexed corpus with no matching documents")
def step_corpus_has_no_match(context) -> None:  # type: ignore[no-untyped-def]
    context.question = _QUESTION
    documents = [
        {
            "chunk_id": "chunk-2",
            "parent_id": "doc-2",
            "title": "Unrelated Topic",
            "heading_path": "Unrelated Topic > Details",
            "content": "This document discusses gardening tips for tomatoes.",
        }
    ]
    _override_rag_service(documents, context)


@when("I ask the RAG endpoint the question")
def step_ask_rag_endpoint(context) -> None:  # type: ignore[no-untyped-def]
    context.response = context.client.post(
        "/api/v1/rag", json={"question": context.question}
    )


@then('the RAG status should be "{status}"')
def step_rag_status(context, status: str) -> None:  # type: ignore[no-untyped-def]
    assert context.response.json()["status"] == status


@then("the response should list at least one numbered source")
def step_at_least_one_source(context) -> None:  # type: ignore[no-untyped-def]
    sources = context.response.json()["sources"]
    assert len(sources) >= 1
    assert sources[0]["number"] == 1


@then("the response should list no sources")
def step_no_sources(context) -> None:  # type: ignore[no-untyped-def]
    assert context.response.json()["sources"] == []


@then("the LLM should not have been called")
def step_llm_not_called(context) -> None:  # type: ignore[no-untyped-def]
    assert context.rag_chat_spy.call_count == 0
