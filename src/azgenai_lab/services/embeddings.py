"""Embedding generation (indexing stage: embed).

Vectorization is owned by this application rather than delegated to the search
service's integrated vectorization, so an embedding failure is distinguishable
from a search failure when retrieval misbehaves.

Batching lives here, above the adapter, and not inside it. The embeddings API
answers a rejected request with a single request-level error and no per-input
detail, so the only way to say anything useful about a failure is to know
exactly which chunks were in the batch that failed.
"""

import hashlib
import logging
import math
from collections.abc import Iterator, Sequence
from typing import Protocol

import openai
from openai import AsyncOpenAI

from azgenai_lab.core.config import Settings
from azgenai_lab.core.errors import (
    ConfigurationError,
    UpstreamError,
    UpstreamServiceError,
    UpstreamThrottledError,
    UpstreamTimeoutError,
)
from azgenai_lab.models.rag import Chunk
from azgenai_lab.models.search_index import EMBEDDING_DIMENSIONS

logger = logging.getLogger(__name__)


class EmbeddingRejectedError(UpstreamError):
    """The embeddings request itself was rejected before anything was indexed.

    This is not the same failure as a per-document indexing error, and it must
    not be routed through that classifier. The embeddings API answers with a
    single request-level error object carrying no per-input index, so when a
    batch is rejected the offending input is unknown: every chunk id in the
    batch is recorded, and the whole document is abandoned before the index is
    touched.

    A 400 does not tell us *why*: an input over the model's token ceiling and a
    malformed request parameter arrive identically. What it does establish is
    that re-sending the same request cannot succeed, which is the only property
    the caller acts on.

    Bisecting the batch to identify the culprit is deliberately not implemented
    — with a batch of at most 36 ids in the log, a person can find it.
    """

    # Indexing is an offline job with no HTTP caller to blame, so this is a 500
    # rather than a 400: if it ever surfaces through an API, the failure is
    # ours. The status code is not the signal here — `retryable` is.
    status_code = 500
    code = "embedding_rejected"
    message = "The embedding request was rejected by the upstream model."
    # Local to this class on purpose; see the note at the head of Task 9.
    retryable = False

    def __init__(
        self,
        upstream_detail: str | None = None,
        *,
        request_id: str | None = None,
        chunk_ids: Sequence[str] = (),
    ) -> None:
        super().__init__(upstream_detail)
        self.request_id = request_id
        self.chunk_ids = tuple(chunk_ids)


# Documented request limits: at most 2,048 inputs, and at most 300,000 tokens
# summed across all inputs — a request over the aggregate limit fails with 400
# even when every individual input is legal. Each input may itself reach 8,192
# tokens.
#
# Without a tokenizer we cannot measure the aggregate, so the batch size is
# derived from the two constants instead:
#
#     floor(300_000 / 8_192) = 36        36 * 8192 = 294,912  <= 300,000
#                                        37 * 8192 = 303,104  >  300,000
#
# 36 is a provable bound, so no safety margin is added on top of it: a smaller
# number would be a magic one.
_MAX_INPUT_TOKENS = 8_192
_MAX_REQUEST_TOKENS = 300_000
MAX_BATCH_INPUTS = math.floor(_MAX_REQUEST_TOKENS / _MAX_INPUT_TOKENS)


class EmbeddingClient(Protocol):
    """One request's worth of inputs. Callers batch; adapters do not."""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class FakeEmbeddingClient:
    """Deterministic vectors derived from a hash of the text.

    These vectors carry **no semantics**: two texts about the same subject are
    no closer than two unrelated ones. The fake exists to exercise wiring,
    batching and the dimension contract. Retrieval quality observed against it
    means nothing — that measurement needs the real model (Day 13).
    """

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [_pseudo_vector(text) for text in texts]


def _pseudo_vector(text: str) -> list[float]:
    values: list[float] = []
    counter = 0
    while len(values) < EMBEDDING_DIMENSIONS:
        digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
        values.extend(byte / 255.0 * 2.0 - 1.0 for byte in digest)
        counter += 1
    return values[:EMBEDDING_DIMENSIONS]


def _batched(chunks: Sequence[Chunk], size: int) -> Iterator[Sequence[Chunk]]:
    for start in range(0, len(chunks), size):
        yield chunks[start : start + size]


async def embed_chunks(client: EmbeddingClient, chunks: Sequence[Chunk]) -> list[list[float]]:
    """Embed every chunk, in order, one request per batch."""
    vectors: list[list[float]] = []
    for batch in _batched(chunks, MAX_BATCH_INPUTS):
        try:
            batch_vectors = await client.embed([chunk.embedding_input for chunk in batch])
        except EmbeddingRejectedError as exc:
            chunk_ids = tuple(chunk.chunk_id for chunk in batch)
            logger.error(
                "embedding batch rejected request_id=%s chunk_ids=%s detail=%s",
                exc.request_id,
                ",".join(chunk_ids),
                exc.upstream_detail,
            )
            raise EmbeddingRejectedError(
                exc.upstream_detail, request_id=exc.request_id, chunk_ids=chunk_ids
            ) from exc
        if len(batch_vectors) != len(batch):
            raise UpstreamServiceError(
                f"embeddings returned {len(batch_vectors)} vectors for {len(batch)} inputs"
            )
        for chunk, vector in zip(batch, batch_vectors, strict=True):
            if len(vector) != EMBEDDING_DIMENSIONS:
                # Fail closed: a vector of the wrong width would be rejected by
                # the index anyway, and writing it is worse than not writing it.
                raise UpstreamServiceError(
                    f"embeddings returned {len(vector)} dimensions for chunk "
                    f"{chunk.chunk_id}; the index expects {EMBEDDING_DIMENSIONS}"
                )
        vectors.extend(batch_vectors)
    return vectors


class AzureOpenAIEmbeddingClient:
    """Embeddings over the v1 GA surface.

    Same base URL as the chat adapter, different endpoint: embeddings are not
    part of the Responses API, and they are billed on their own per-token rate.
    ``model`` is the deployment name, not the model name.
    """

    def __init__(self, settings: Settings) -> None:
        if not settings.azure_openai_endpoint or not settings.azure_openai_api_key:
            raise ConfigurationError("azure_openai_endpoint and api key are required")
        if not settings.azure_openai_embedding_deployment:
            raise ConfigurationError(
                "azure_openai_embedding_deployment is required when "
                "use_fake_embeddings is false"
            )
        self._deployment = settings.azure_openai_embedding_deployment
        self._client = AsyncOpenAI(
            base_url=f"{settings.azure_openai_endpoint.rstrip('/')}/openai/v1/",
            api_key=settings.azure_openai_api_key.get_secret_value(),
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        try:
            response = await self._client.embeddings.create(
                model=self._deployment,
                input=list(texts),
                dimensions=EMBEDDING_DIMENSIONS,
            )
        # Order matters: these are all APIStatusError subclasses, so the
        # specific handlers must precede the general one.
        except (
            openai.AuthenticationError,
            openai.PermissionDeniedError,
            openai.NotFoundError,
        ) as exc:
            # Bad key, missing role assignment, wrong deployment name — our
            # deployment is broken, and no chunk is at fault.
            raise ConfigurationError(str(exc)) from exc
        except openai.BadRequestError as exc:
            raise EmbeddingRejectedError(str(exc), request_id=exc.request_id) from exc
        except openai.RateLimitError as exc:
            raise UpstreamThrottledError(str(exc)) from exc
        except openai.APITimeoutError as exc:
            raise UpstreamTimeoutError(str(exc)) from exc
        # Catch-all: subsumes APIStatusError (any status not already mapped
        # above), APIConnectionError, and the rest of OpenAIError's subtree —
        # e.g. APIResponseValidationError, raised when a 2xx response body
        # fails schema validation (a gateway mangling the body, or an
        # upstream shape change). Nothing SDK-specific may escape this
        # boundary, so this must stay last and unconditional.
        except openai.OpenAIError as exc:
            raise UpstreamServiceError(str(exc)) from exc

        logger.info(
            "embeddings call deployment=%s inputs=%d prompt_tokens=%s total_tokens=%s",
            self._deployment,
            len(texts),
            response.usage.prompt_tokens,
            response.usage.total_tokens,
        )
        return [item.embedding for item in response.data]


def build_embedding_client(settings: Settings) -> EmbeddingClient:
    """The one place fake and real are chosen. Handlers never branch on this."""
    if settings.use_fake_embeddings:
        return FakeEmbeddingClient()
    return AzureOpenAIEmbeddingClient(settings)


__all__ = [
    "MAX_BATCH_INPUTS",
    "AzureOpenAIEmbeddingClient",
    "EmbeddingClient",
    "EmbeddingRejectedError",
    "FakeEmbeddingClient",
    "UpstreamError",
    "build_embedding_client",
    "embed_chunks",
]
