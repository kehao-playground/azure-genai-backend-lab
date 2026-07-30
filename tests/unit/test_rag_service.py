"""Augment + Generate: context shaping, the structural no-answer path, and
usage/incomplete-reason plumbing through to the caller.
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import replace

from azgenai_lab.models.conversation import ReplayItem
from azgenai_lab.models.search import SearchHit
from azgenai_lab.prompts.loader import load_prompt
from azgenai_lab.services.azure_openai import ChatResult, ChatStreamEvent, FakeChatService
from azgenai_lab.services.azure_search import FakeSearchClient
from azgenai_lab.services.embeddings import FakeEmbeddingClient
from azgenai_lab.services.rag import RagService, render_sources, render_user_message
from azgenai_lab.services.retrieval import Retriever

HIT = SearchHit(
    chunk_id="doc-a-0000",
    parent_id="doc-a",
    title="Doc A",
    heading_path="Doc A > Intro",
    content="alpha beta",
    score=1.0,
)

DOC = {
    "chunk_id": "doc-a-0000",
    "parent_id": "doc-a",
    "title": "Doc A",
    "heading_path": "Doc A > Intro",
    "content": "alpha beta",
}


class ExplodingChat:
    """A ChatService whose methods must never be called (proves the
    zero-hits short-circuit never reaches the LLM)."""

    async def complete(self, items: Sequence[ReplayItem]) -> ChatResult:
        raise AssertionError("LLM must not be called")

    async def open_stream(self, items: Sequence[ReplayItem]) -> AsyncIterator[ChatStreamEvent]:
        raise AssertionError("LLM must not be called")


def test_render_sources_numbers_from_one_with_heading_path() -> None:
    text = render_sources([HIT, replace(HIT, chunk_id="doc-a-0001", content="gamma")])
    assert text.startswith("[1] Doc A > Intro\nalpha beta")
    assert "\n\n[2] Doc A > Intro\ngamma" in text


def test_render_user_message_wraps_sources_and_question() -> None:
    text = render_user_message("what is alpha?", [HIT])
    assert text.index("[1]") < text.index("Question: what is alpha?")


async def test_answer_returns_no_answer_without_calling_llm_on_zero_hits() -> None:
    service = RagService(
        Retriever(FakeEmbeddingClient(), FakeSearchClient([]), top=5), ExplodingChat()
    )
    result = await service.answer("anything")
    assert result.status == "no_answer"
    assert result.answer is None
    assert result.hits == ()
    assert result.usage is None
    assert result.incomplete_reason is None


async def test_answer_grounds_single_turn_and_carries_usage() -> None:
    fake_chat = FakeChatService(prompt=load_prompt("rag_answer"))
    service = RagService(
        Retriever(FakeEmbeddingClient(), FakeSearchClient([DOC]), top=5), fake_chat
    )
    result = await service.answer("alpha")
    assert result.status == "answered"
    assert result.answer is not None
    assert "prompt=rag_answer@1" in result.answer
    assert "history=" not in result.answer  # single item: no conversation replay
    assert result.usage is not None
    assert result.usage.total_tokens > 0
    assert [hit.chunk_id for hit in result.hits] == ["doc-a-0000"]
