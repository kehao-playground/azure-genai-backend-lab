"""Retrieve step of the RAG pipeline: question -> ranked chunks.

Composes the Day 12 embedding adapter and the Day 13 search adapter. Mode is
fixed to hybrid: Day 13 measured that keyword-only and vector-only each miss
queries the other catches, and the semantic ranker stays out of the default
path (single-probe result; documentation conflict unresolved).
"""

import logging
import time

from opentelemetry.semconv._incubating.attributes import gen_ai_attributes

from azgenai_lab.core.config import Settings
from azgenai_lab.core.telemetry import FAKE_EMBEDDING_DEPLOYMENT, stage_span
from azgenai_lab.models.principal import Principal
from azgenai_lab.models.search import SearchMode, SearchResult
from azgenai_lab.services.azure_search import SearchClient, build_search_client
from azgenai_lab.services.embeddings import EmbeddingClient, build_embedding_client

logger = logging.getLogger(__name__)


class Retriever:
    def __init__(
        self,
        embedding_client: EmbeddingClient,
        search_client: SearchClient,
        *,
        top: int,
        embedding_deployment: str = FAKE_EMBEDDING_DEPLOYMENT,
    ) -> None:
        self._embedding_client = embedding_client
        self._search_client = search_client
        self._top = top
        # Named before the call, so it cannot be read off the result. The
        # default is the fake sentinel, matching the precedent Day 22 set:
        # the value says which boundary was reached, not that Azure was.
        self._embedding_deployment = embedding_deployment

    async def retrieve(self, question: str, principal: Principal) -> SearchResult:
        embed_started = time.perf_counter()
        # Spanned at the call site rather than inside each adapter: embed()
        # has a fake and a real implementation and this is the one place that
        # knows what the call means. Same reasoning as TracingChatService,
        # applied without a second decorator.
        try:
            with stage_span(f"embeddings {self._embedding_deployment}") as span:
                span.set_attribute(gen_ai_attributes.GEN_AI_OPERATION_NAME, "embeddings")
                span.set_attribute(
                    gen_ai_attributes.GEN_AI_REQUEST_MODEL, self._embedding_deployment
                )
                vector = (await self._embedding_client.embed([question]))[0]
        except Exception as exc:
            duration_ms = (time.perf_counter() - embed_started) * 1000
            # Redaction: question text is never logged here or below, only
            # counts/dims/durations and the exception class name (Day 14 r06
            # residual 3 -- failure paths previously logged nothing).
            logger.info(
                "rag stage=embed_query duration_ms=%.1f outcome=error exception=%s",
                duration_ms,
                type(exc).__name__,
            )
            raise
        duration_ms = (time.perf_counter() - embed_started) * 1000
        # Redaction: question text is never logged here, only its embedding's
        # dimensionality and how long the call took (Day 14 r04 residual B).
        # The Search adapter already logs its own latency; that line stands
        # as-is.
        logger.info(
            "rag stage=embed_query duration_ms=%.1f outcome=success vector_dims=%d",
            duration_ms,
            len(vector),
        )
        search_started = time.perf_counter()
        try:
            with stage_span("azure.search.query") as span:
                result = await self._search_client.search(
                    question, vector, mode=SearchMode.HYBRID, top=self._top, principal=principal
                )
                # Hit count and nothing else. The OData filter this query
                # carries is built from group ids (Day 15), and none of that
                # goes near a span attribute.
                span.set_attribute("azgenai.search.hit_count", len(result.hits))
        except Exception as exc:
            duration_ms = (time.perf_counter() - search_started) * 1000
            # New line, distinct from the adapter's own internal logging
            # (which does not fire reliably on failure and is not in the
            # "rag stage=" vocabulary): Day 14 r06 residual 3.
            logger.info(
                "rag stage=search duration_ms=%.1f outcome=error exception=%s",
                duration_ms,
                type(exc).__name__,
            )
            raise
        duration_ms = (time.perf_counter() - search_started) * 1000
        logger.info(
            "rag stage=search duration_ms=%.1f outcome=success hit_count=%d",
            duration_ms,
            len(result.hits),
        )
        return result

    async def aclose(self) -> None:
        """Close both composed clients. Each adapter's own aclose() is
        idempotent. Isolated: an embedding-client close failure must not
        strand the search client's own httpx pool -- the same discipline
        Day 14 review finding 4 established, applied here since it was
        missing at this layer too (Day 23 review, third wave)."""
        try:
            await self._embedding_client.aclose()
        finally:
            await self._search_client.aclose()


def build_retriever(settings: Settings) -> Retriever:
    return Retriever(
        build_embedding_client(settings),
        build_search_client(settings),
        top=settings.rag_top,
        embedding_deployment=(
            FAKE_EMBEDDING_DEPLOYMENT
            if settings.use_fake_embeddings
            else (settings.azure_openai_embedding_deployment or FAKE_EMBEDDING_DEPLOYMENT)
        ),
    )


__all__ = [
    "Retriever",
    "build_retriever",
]
