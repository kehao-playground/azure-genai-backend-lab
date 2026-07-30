"""Augment + Generate: context shaping, the structural no-answer path, and
usage/incomplete-reason plumbing through to the caller.
"""

import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace

import pytest

from azgenai_lab.models.conversation import ReplayItem
from azgenai_lab.models.search import SearchHit, SearchMode, SearchResult
from azgenai_lab.prompts.loader import load_prompt
from azgenai_lab.services.azure_openai import ChatResult, ChatStreamEvent, FakeChatService
from azgenai_lab.services.azure_search import FakeSearchClient
from azgenai_lab.services.embeddings import FakeEmbeddingClient
from azgenai_lab.services.rag import (
    MAX_PROMPT_BYTES,
    RagContextOverflowError,
    RagService,
    render_sources,
    render_user_message,
)
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


class _StubOversizedSearchClient:
    """Duck-typed SearchClient: returns a fixed, deliberately hostile set of
    hits regardless of query, so the budget-selection logic can be tested
    without routing through FakeSearchClient's keyword-overlap matching.
    """

    def __init__(self, hits: Sequence[SearchHit]) -> None:
        self._hits = tuple(hits)

    async def search(
        self,
        query_text: str,
        query_vector: Sequence[float] | None = None,
        *,
        mode: SearchMode = SearchMode.HYBRID,
        top: int,
        filter: str | None = None,
        vector_k: int = 50,
    ) -> SearchResult:
        return SearchResult(hits=self._hits, mode=mode, vector_k=vector_k)

    async def aclose(self) -> None:
        pass


class _RecordingChatService:
    """Records the byte size of exactly what it was called with, so tests can
    assert on the wire-level budget without a real upstream call."""

    def __init__(self) -> None:
        self.received_items: Sequence[ReplayItem] | None = None

    async def complete(self, items: Sequence[ReplayItem]) -> ChatResult:
        self.received_items = items
        return ChatResult(
            message="ok",
            usage=None,
        )

    async def open_stream(self, items: Sequence[ReplayItem]) -> AsyncIterator[ChatStreamEvent]:
        raise AssertionError("this test never streams")

    async def aclose(self) -> None:
        pass


def _oversized_hits(count: int = 50) -> list[SearchHit]:
    # Worst legal case per Day 14 r04: 50 hits x 2,000 U+10FFFF chars each
    # (4 UTF-8 bytes per char), the maximum a within-contract chunk can carry.
    hostile_content = "\U0010ffff" * 2000
    return [
        SearchHit(
            chunk_id=f"doc-{i:04d}",
            parent_id=f"doc-{i:04d}",
            title=f"Doc {i}",
            heading_path=f"Doc {i}",
            content=hostile_content,
            score=1.0,
        )
        for i in range(count)
    ]


async def test_answer_truncates_hostile_oversized_corpus_to_fit_prompt_budget() -> None:
    hits = _oversized_hits(50)
    prompt = load_prompt("rag_answer")
    instructions_bytes = len(prompt.text.encode("utf-8"))
    retriever = Retriever(
        FakeEmbeddingClient(), _StubOversizedSearchClient(hits), top=50
    )
    chat = _RecordingChatService()
    service = RagService(retriever, chat, instructions_bytes=instructions_bytes)

    question = "q" * 2000
    result = await service.answer(question)

    assert result.status == "answered"
    assert len(result.hits) < 50
    assert chat.received_items is not None
    message_bytes = sum(
        len(str(item.get("content", "")).encode("utf-8")) for item in chat.received_items
    )
    assert instructions_bytes + message_bytes <= MAX_PROMPT_BYTES

    # sources reflect exactly what generation saw, and citation numbering
    # stays contiguous 1..len(included).
    rendered = str(chat.received_items[0]["content"])
    expected_numbers = [f"[{n}]" for n in range(1, len(result.hits) + 1)]
    for marker in expected_numbers:
        assert marker in rendered
    assert f"[{len(result.hits) + 1}]" not in rendered


async def test_answer_raises_context_overflow_when_first_hit_content_too_big() -> None:
    # Live SearchHit.content has no runtime maximum (only the offline chunker
    # bounds it) -- an oversized first hit must not escape as an unhandled
    # AssertionError / plain-text 500.
    oversized_hit = replace(HIT, content="x" * (MAX_PROMPT_BYTES + 1))
    retriever = Retriever(
        FakeEmbeddingClient(), _StubOversizedSearchClient([oversized_hit]), top=5
    )
    chat = _RecordingChatService()
    service = RagService(retriever, chat)

    with pytest.raises(RagContextOverflowError):
        await service.answer("q")

    assert chat.received_items is None  # zero provider calls


async def test_answer_raises_context_overflow_when_first_hit_heading_path_too_big() -> None:
    oversized_hit = replace(HIT, heading_path="h" * (MAX_PROMPT_BYTES + 1))
    retriever = Retriever(
        FakeEmbeddingClient(), _StubOversizedSearchClient([oversized_hit]), top=5
    )
    chat = _RecordingChatService()
    service = RagService(retriever, chat)

    with pytest.raises(RagContextOverflowError):
        await service.answer("q")

    assert chat.received_items is None


async def test_answer_includes_all_hits_when_within_budget() -> None:
    fake_chat = FakeChatService(prompt=load_prompt("rag_answer"))
    service = RagService(
        Retriever(FakeEmbeddingClient(), FakeSearchClient([DOC]), top=5),
        fake_chat,
        instructions_bytes=10,
    )
    result = await service.answer("alpha")
    assert result.status == "answered"
    assert len(result.hits) == 1


async def test_answer_logs_dropped_source_count_on_truncation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hits = _oversized_hits(50)
    prompt = load_prompt("rag_answer")
    instructions_bytes = len(prompt.text.encode("utf-8"))
    retriever = Retriever(FakeEmbeddingClient(), _StubOversizedSearchClient(hits), top=50)
    chat = _RecordingChatService()
    service = RagService(retriever, chat, instructions_bytes=instructions_bytes)

    with caplog.at_level(logging.INFO, logger="azgenai_lab.services.rag"):
        result = await service.answer("q" * 2000)

    record = next(r for r in caplog.records if r.name == "azgenai_lab.services.rag")
    message = record.getMessage()
    dropped = 50 - len(result.hits)
    assert f"dropped_source_count={dropped}" in message
    assert dropped > 0
