import re
from collections.abc import Sequence

from behave import given, then, when

from azgenai_lab.api.rag import get_rag_service
from azgenai_lab.main import app
from azgenai_lab.models.conversation import ReplayItem
from azgenai_lab.services.azure_openai import ChatResult
from azgenai_lab.services.azure_search import FakeSearchClient
from azgenai_lab.services.embeddings import FakeEmbeddingClient
from azgenai_lab.services.rag import RagService
from azgenai_lab.services.retrieval import Retriever

_QUESTION = "What does hybrid search combine?"

# A fixed, independently authored answer: NOT derived from the input items,
# so citing it as evidence of model-generated citations is honest (the
# no-longer-used echoing FakeChatService would have made that claim false).
_STUBBED_ANSWER = "Alpha pairs with beta [1]."


class StubChatService:
    """Protocol-compatible fake that returns a fixed, input-independent
    cited answer and counts how many times it was called."""

    def __init__(self, answer: str = _STUBBED_ANSWER) -> None:
        self._answer = answer
        self.call_count = 0

    async def complete(self, items: Sequence[ReplayItem]) -> ChatResult:
        self.call_count += 1
        return ChatResult(message=self._answer, model_version="stub")

    async def aclose(self) -> None:
        """No-op: this stub owns no resources to release."""


def _override_rag_service(  # type: ignore[no-untyped-def]
    documents: Sequence[dict[str, str]], context, answer: str = _STUBBED_ANSWER
) -> None:
    context.rag_documents = documents
    search_client = FakeSearchClient(documents)
    retriever = Retriever(FakeEmbeddingClient(), search_client, top=5)
    chat = StubChatService(answer)
    context.rag_chat_spy = chat
    # StubChatService never actually sends the rag_answer prompt (it returns
    # a fixed answer, independent of any prompt text), but the answered-path
    # audit event (Day 22) still needs an attribution present -- reuse the
    # real value the app's own startup composition produced (matches Task
    # 6/7's precedent for /chat: app.state.conversation_service.audit_attribution)
    # rather than fabricating one.
    service = RagService(
        retriever, chat, audit_attribution=app.state.rag_service.audit_attribution
    )
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
            "tenant_id": "t1",
            "allowed_groups": [],
        }
    ]
    _override_rag_service(documents, context)


@given("retrieval that returns zero hits")
def step_retrieval_returns_zero_hits(context) -> None:  # type: ignore[no-untyped-def]
    context.question = _QUESTION
    # Honest zero-hit fixture: an empty corpus, not an unrelated document
    # relying on lexical non-matching. The contract under test is purely
    # structural (zero hits -> no_answer, no LLM call), not semantic.
    _override_rag_service([], context)


@given("a model answer citing one real source and one invented source")
def step_answer_with_invented_citation(context) -> None:  # type: ignore[no-untyped-def]
    # [7] cannot exist: the corpus above yields exactly one source. Same
    # fixed, input-independent stub as the scenario above, with an
    # out-of-range marker added.
    _override_rag_service(
        context.rag_documents, context, answer="Alpha pairs with beta [1], and [7]."
    )


@when("I ask the RAG endpoint the question")
def step_ask_rag_endpoint(context) -> None:  # type: ignore[no-untyped-def]
    context.response = context.client.post(
        "/api/v1/rag", json={"question": context.question}
    )


@when("I ask the RAG endpoint a whitespace-only question")
def step_ask_rag_endpoint_whitespace_only(context) -> None:  # type: ignore[no-untyped-def]
    context.response = context.client.post("/api/v1/rag", json={"question": "   "})


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


@then("every citation number in the answer should reference a returned source")
def step_citations_reference_sources(context) -> None:  # type: ignore[no-untyped-def]
    body = context.response.json()
    answer = body["answer"]
    sources = body["sources"]
    citation_numbers = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
    # Mechanical validation only: every [n] marker falls within the range of
    # returned sources. This is not a semantic-entailment claim.
    assert citation_numbers, "expected at least one [n] citation marker in the answer"
    assert citation_numbers <= set(range(1, len(sources) + 1))


@then("the response should report {count:d} distinct cited source")
@then("the response should report {count:d} distinct cited sources")
def step_cited_source_count(context, count: int) -> None:  # type: ignore[no-untyped-def]
    assert context.response.json()["cited_source_count"] == count


@then("the response should report {count:d} stripped citation")
@then("the response should report {count:d} stripped citations")
def step_stripped_citation_count(context, count: int) -> None:  # type: ignore[no-untyped-def]
    assert context.response.json()["stripped_citation_count"] == count


@then("both citation counts should be null")
def step_citation_counts_null(context) -> None:  # type: ignore[no-untyped-def]
    # Null, not 0: there is no answer text for a count to describe, and 0
    # would read as "an answer that cited nothing".
    body = context.response.json()
    assert body["cited_source_count"] is None
    assert body["stripped_citation_count"] is None
