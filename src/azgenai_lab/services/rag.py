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
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from azgenai_lab.core.config import Settings
from azgenai_lab.core.errors import UpstreamError
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
# this caps total UTF-8 *bytes* of the counted TEXT sent as provider input.
# Proof (exact, for the text it covers): every token in a BPE vocabulary
# decodes to at least one UTF-8 byte, so for any text, token_count <=
# utf8_byte_count. That bound covers the instructions + rendered sources +
# question text we assemble -- it does NOT cover the Responses API's message
# framing (roles, field wrappers, protocol overhead), which sits outside the
# text we count and has no documented byte/token bound of its own. This is
# therefore a conservative text-byte guardrail, not a full mathematical proof
# of the complete provider-input ceiling.
#
# PROMPT_FRAMING_HEADROOM_BYTES reserves budget for that unbounded framing
# overhead: chosen well above any observed framing overhead, equivalent to
# reserving >=4,096 framing tokens. If provider-side overflow ever occurs
# despite this headroom, it remains a server-owned failure surfaced as
# RagContextOverflowError (see below) rather than an uncaught upstream error.
PROMPT_FRAMING_HEADROOM_BYTES = 4_096
MAX_PROMPT_BYTES = PROMPT_INPUT_TOKEN_LIMIT - PROMPT_FRAMING_HEADROOM_BYTES


class RagContextOverflowError(UpstreamError):
    """Even the single highest-ranked hit does not fit MAX_PROMPT_BYTES.

    Server-owned, not client-owned: the corpus is server-selected (indexing
    is our pipeline, not user input), so a hit this large is our failure to
    enforce an indexing-side invariant, not a bad request. The offline
    chunker (Day 12) makes this unreachable for our own corpus -- chunks are
    bounded to chunk_max_chars=2000 U+10FFFF-worst-case chars, far under this
    budget -- but the query boundary (this module) must not *trust* an
    indexing invariant it does not itself enforce. Live SearchHit.content /
    heading_path have no runtime maximum, so this is the honest fallback for
    a hostile or malformed index entry, rather than an unhandled
    AssertionError escaping as a plain-text 500.
    """

    status_code = 500
    code = "rag_context_overflow"
    message = "The retrieved context could not fit the model input budget."


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

    For our own corpus this always includes at least the rank-1 hit: the
    offline chunker (Day 12) bounds chunk_max_chars=2000, far under the
    budget. But `hits` here come from a live SearchHit with no runtime
    maximum on `content`/`heading_path` -- the query boundary must not
    *trust* that indexing-side invariant. If even the rank-1 hit cannot fit,
    that is a server-owned failure (the corpus is server-selected), raised
    as `RagContextOverflowError` rather than an unhandled AssertionError.
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
        first = hits[0]
        first_bytes = len(render_user_message(question, [first]).encode("utf-8"))
        raise RagContextOverflowError(
            upstream_detail=f"chunk_id={first.chunk_id} bytes={first_bytes}"
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
        total_started = time.perf_counter()
        current_stage = "retrieve"
        try:
            retrieved = await self._retriever.retrieve(question)
            if not retrieved.hits:
                _log_rag_stage(question, retrieved.hits, context="", dropped_source_count=0)
                # No provider call happens on this path (structural
                # short-circuit), so there is no generation duration to
                # report -- omitted rather than logged as a fake 0ms.
                logger.info(
                    "rag stage=complete total_ms=%.1f status=no_answer outcome=success",
                    (time.perf_counter() - total_started) * 1000,
                )
                return RagAnswer(
                    status="no_answer", answer=None, hits=(), usage=None, incomplete_reason=None
                )
            current_stage = "assemble_context"
            included, dropped_source_count = _select_within_budget(
                question, retrieved.hits, instructions_bytes=self._instructions_bytes
            )
            user_message = render_user_message(question, included)
            _log_rag_stage(
                question,
                included,
                context=user_message,
                dropped_source_count=dropped_source_count,
            )
            item = {"role": "user", "content": user_message}
            current_stage = "generation"
            generation_started = time.perf_counter()
            try:
                result = await self._chat_service.complete([item])
            except Exception as exc:
                duration_ms = (time.perf_counter() - generation_started) * 1000
                # Redaction: neither question text nor chunk content is
                # logged here -- only duration and the exception class name
                # (Day 14 r06 residual 3).
                logger.info(
                    "rag stage=generation duration_ms=%.1f outcome=error exception=%s",
                    duration_ms,
                    type(exc).__name__,
                )
                raise
            logger.info(
                "rag stage=generation duration_ms=%.1f outcome=success",
                (time.perf_counter() - generation_started) * 1000,
            )
        except Exception as exc:
            logger.info(
                "rag stage=complete total_ms=%.1f status=error outcome=error "
                "failed_stage=%s exception=%s",
                (time.perf_counter() - total_started) * 1000,
                current_stage,
                type(exc).__name__,
            )
            raise
        logger.info(
            "rag stage=complete total_ms=%.1f status=answered outcome=success",
            (time.perf_counter() - total_started) * 1000,
        )
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
    "PROMPT_FRAMING_HEADROOM_BYTES",
    "PROMPT_INPUT_TOKEN_LIMIT",
    "RagAnswer",
    "RagContextOverflowError",
    "RagService",
    "RagStatus",
    "build_rag_service",
    "render_sources",
    "render_user_message",
]
