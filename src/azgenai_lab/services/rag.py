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

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from azgenai_lab.core.config import Settings
from azgenai_lab.models.chat import TokenUsage
from azgenai_lab.models.search import SearchHit
from azgenai_lab.services.azure_openai import ChatService, IncompleteReason, build_chat_service
from azgenai_lab.services.retrieval import Retriever, build_retriever

logger = logging.getLogger(__name__)

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


def _log_rag_stage(question: str, hits: Sequence[SearchHit], context: str) -> None:
    # Bridges the two stages Day 8/13 already log (search's per-call line,
    # the LLM adapter's prompt/usage lines): what retrieval handed to
    # augmentation, and how much of it there was. core.logging's record
    # factory (Day 14 review finding 5) stamps correlation_id on this line
    # automatically, so no manual read/extra is needed here to keep it
    # joinable with the other two stages.
    #
    # Redaction: question text and chunk content are never logged, here or
    # anywhere else in this module — only counts, ids, and character lengths.
    # Both are user-submitted/corpus-sourced text that may be sensitive; the
    # stage line exists to debug retrieval shape, not to double as a content
    # log.
    content_lengths = ",".join(str(len(hit.content)) for hit in hits)
    logger.info(
        "rag stage=assemble_context hits=%d chunk_ids=%s content_chars=%s "
        "context_chars=%d question_chars=%d",
        len(hits),
        ",".join(hit.chunk_id for hit in hits),
        content_lengths,
        len(context),
        len(question),
    )


class RagService:
    def __init__(self, retriever: Retriever, chat_service: ChatService) -> None:
        self._retriever = retriever
        self._chat_service = chat_service

    async def answer(self, question: str) -> RagAnswer:
        retrieved = await self._retriever.retrieve(question)
        if not retrieved.hits:
            _log_rag_stage(question, retrieved.hits, context="")
            return RagAnswer(
                status="no_answer", answer=None, hits=(), usage=None, incomplete_reason=None
            )
        user_message = render_user_message(question, retrieved.hits)
        _log_rag_stage(question, retrieved.hits, context=user_message)
        item = {"role": "user", "content": user_message}
        result = await self._chat_service.complete([item])
        return RagAnswer(
            status="answered",
            answer=result.message,
            hits=retrieved.hits,
            usage=result.usage,
            incomplete_reason=result.incomplete_reason,
        )

    async def aclose(self) -> None:
        """Close the composed retriever and chat service."""
        await self._retriever.aclose()
        await self._chat_service.aclose()


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
