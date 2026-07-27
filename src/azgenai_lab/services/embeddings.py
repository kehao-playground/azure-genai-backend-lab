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

from azgenai_lab.core.errors import UpstreamError, UpstreamServiceError
from azgenai_lab.models.rag import Chunk
from azgenai_lab.models.search_index import EMBEDDING_DIMENSIONS

logger = logging.getLogger(__name__)

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
        batch_vectors = await client.embed([chunk.embedding_input for chunk in batch])
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


__all__ = [
    "MAX_BATCH_INPUTS",
    "EmbeddingClient",
    "FakeEmbeddingClient",
    "UpstreamError",
    "embed_chunks",
]
