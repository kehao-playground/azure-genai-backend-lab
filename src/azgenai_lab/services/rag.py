"""Augment + Generate steps of the RAG pipeline (Day 14).

Grounding is structural where it can be and instructional where it cannot:
zero retrieved hits short-circuit to a no-answer response without touching
the LLM (Day 13 showed RRF scores cannot tell answer-present from
answer-absent, so there is no score threshold to hide behind); with hits,
the rag_answer prompt instructs citation and refusal, and a model-level
refusal still comes back as status "answered" — an honest gap, not a bug.

Retrieved content is untrusted input. It travels in the user message as
data, and the instructions live in the template; the template explicitly
marks sources as non-instructions. That is mitigation, not immunity —
prompt injection via poisoned corpus stays on the threat-model page.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from azgenai_lab.core.config import Settings
from azgenai_lab.models.chat import TokenUsage
from azgenai_lab.models.search import SearchHit
from azgenai_lab.services.azure_openai import ChatService, IncompleteReason, build_chat_service
from azgenai_lab.services.retrieval import Retriever, build_retriever

RagStatus = Literal["answered", "no_answer"]


@dataclass(frozen=True)
class RagAnswer:
    status: RagStatus
    answer: str | None
    hits: tuple[SearchHit, ...]
    usage: TokenUsage | None
    incomplete_reason: IncompleteReason | None


def render_sources(hits: Sequence[SearchHit]) -> str:
    # heading_path already starts with the document title (Day 12 invariant),
    # so one line locates the chunk; content stays verbatim — it is the text
    # a citation points at (embedding input != citation text).
    return "\n\n".join(
        f"[{number}] {hit.heading_path}\n{hit.content}"
        for number, hit in enumerate(hits, start=1)
    )


def render_user_message(question: str, hits: Sequence[SearchHit]) -> str:
    return f"Sources:\n\n{render_sources(hits)}\n\nQuestion: {question}"


class RagService:
    def __init__(self, retriever: Retriever, chat_service: ChatService) -> None:
        self._retriever = retriever
        self._chat_service = chat_service

    async def answer(self, question: str) -> RagAnswer:
        retrieved = await self._retriever.retrieve(question)
        if not retrieved.hits:
            return RagAnswer(
                status="no_answer", answer=None, hits=(), usage=None, incomplete_reason=None
            )
        item = {"role": "user", "content": render_user_message(question, retrieved.hits)}
        result = await self._chat_service.complete([item])
        return RagAnswer(
            status="answered",
            answer=result.message,
            hits=retrieved.hits,
            usage=result.usage,
            incomplete_reason=result.incomplete_reason,
        )


def build_rag_service(settings: Settings) -> RagService:
    return RagService(
        build_retriever(settings), build_chat_service(settings, prompt_name="rag_answer")
    )


__all__ = [
    "RagAnswer",
    "RagService",
    "RagStatus",
    "build_rag_service",
    "render_sources",
    "render_user_message",
]
