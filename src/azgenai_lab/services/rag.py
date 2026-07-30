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
from azgenai_lab.prompts.loader import load_prompt
from azgenai_lab.services.azure_openai import ChatService, IncompleteReason, build_chat_service
from azgenai_lab.services.retrieval import Retriever, build_retriever

logger = logging.getLogger(__name__)

RagStatus = Literal["answered", "no_answer"]

# gpt-5-mini 2025-08-07's documented input limit (Microsoft docs, checked
# 2026-07-30). Worst legal case per the API contract (RAG_TOP<=50 hits of
# chunk_max_chars<=2,000 U+10FFFF chars each, plus a 2,000-char question)
# totals well past this in tokens, so an unbounded assembled prompt can
# trigger the provider's own context_length_exceeded rejection.
PROMPT_INPUT_TOKEN_LIMIT = 272_000

# Day 9 "meter, don't estimate": no tokenizer is used here either. Instead
# this caps total UTF-8 *bytes* sent as provider input. Proof: every token in
# a BPE vocabulary decodes to at least one UTF-8 byte, so for any input,
# token_count <= utf8_byte_count. Capping total input bytes at
# PROMPT_INPUT_TOKEN_LIMIT therefore provably caps the token count at or
# below the documented input limit too — a conservative, tokenizer-free bound.
MAX_PROMPT_BYTES = PROMPT_INPUT_TOKEN_LIMIT


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


def _log_rag_stage(
    question: str, hits: Sequence[SearchHit], context: str, *, dropped_source_count: int
) -> None:
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
    #
    # dropped_source_count (Day 14 r04 residual A): hits retrieved but
    # excluded from the prompt by the byte budget in `_select_within_budget`.
    # `hits` here is already the *included* subset, so this field is the only
    # place the drop is visible.
    content_lengths = ",".join(str(len(hit.content)) for hit in hits)
    logger.info(
        "rag stage=assemble_context hits=%d chunk_ids=%s content_chars=%s "
        "context_chars=%d question_chars=%d dropped_source_count=%d",
        len(hits),
        ",".join(hit.chunk_id for hit in hits),
        content_lengths,
        len(context),
        len(question),
        dropped_source_count,
    )


def _select_within_budget(
    question: str, hits: Sequence[SearchHit], *, instructions_bytes: int
) -> tuple[list[SearchHit], int]:
    """Include hits in rank order until the rendered user message would push
    the total provider input (instructions + message) past MAX_PROMPT_BYTES.

    Stops at the first hit that does not fit rather than skipping it and
    trying a lower-ranked one: including a lower-ranked source while
    excluding a higher-ranked one would misrepresent the ranking the caller
    asked retrieval for.

    Invariant: at least one hit always fits. `instructions_bytes` (the
    rag_answer prompt) is small (well under ~10.5k bytes observed); a single
    hit's rendered contribution is bounded by chunk_max_chars=2000 chars at
    <=4 UTF-8 bytes/char (<=8k bytes) plus a short heading/label overhead.
    MAX_PROMPT_BYTES=272,000 leaves enormous headroom above that combined
    floor, so the loop below cannot legitimately produce zero included hits
    when `hits` is non-empty.
    """
    budget = MAX_PROMPT_BYTES - instructions_bytes
    included: list[SearchHit] = []
    for hit in hits:
        candidate = [*included, hit]
        rendered_bytes = len(render_user_message(question, candidate).encode("utf-8"))
        if rendered_bytes > budget:
            break
        included.append(hit)
    if hits and not included:
        raise AssertionError(
            "prompt budget invariant violated: no hit fit within MAX_PROMPT_BYTES "
            "even though per-hit and instructions sizes are bounded well under it"
        )
    return included, len(hits) - len(included)


class RagService:
    def __init__(
        self, retriever: Retriever, chat_service: ChatService, *, instructions_bytes: int = 0
    ) -> None:
        self._retriever = retriever
        self._chat_service = chat_service
        # Byte cost of the rag_answer prompt's instructions text (Day 14 r04
        # residual A). This service only needs the byte count to budget the
        # assembled prompt; the ChatService adapter is what actually owns and
        # sends the instructions text on the wire.
        self._instructions_bytes = instructions_bytes

    async def answer(self, question: str) -> RagAnswer:
        retrieved = await self._retriever.retrieve(question)
        if not retrieved.hits:
            _log_rag_stage(question, retrieved.hits, context="", dropped_source_count=0)
            return RagAnswer(
                status="no_answer", answer=None, hits=(), usage=None, incomplete_reason=None
            )
        included, dropped_source_count = _select_within_budget(
            question, retrieved.hits, instructions_bytes=self._instructions_bytes
        )
        user_message = render_user_message(question, included)
        _log_rag_stage(
            question, included, context=user_message, dropped_source_count=dropped_source_count
        )
        item = {"role": "user", "content": user_message}
        result = await self._chat_service.complete([item])
        return RagAnswer(
            status="answered",
            answer=result.message,
            hits=tuple(included),
            usage=result.usage,
            incomplete_reason=result.incomplete_reason,
        )

    async def aclose(self) -> None:
        """Close the composed retriever and chat service."""
        await self._retriever.aclose()
        await self._chat_service.aclose()


def build_rag_service(settings: Settings) -> RagService:
    prompt = load_prompt("rag_answer")
    return RagService(
        build_retriever(settings),
        build_chat_service(settings, prompt_name="rag_answer"),
        # build_chat_service (above) loads its own copy of the same template
        # to hand to the adapter that owns the instructions wire-side; this
        # is a second, deterministic load of the same file solely so
        # RagService can know the instructions' byte cost for budgeting.
        instructions_bytes=len(prompt.text.encode("utf-8")),
    )


__all__ = [
    "RagAnswer",
    "RagService",
    "RagStatus",
    "build_rag_service",
    "render_sources",
    "render_user_message",
]
