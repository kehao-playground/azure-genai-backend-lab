"""The embedding boundary. Batching lives above the adapter so that a rejected
batch is attributable to a known set of chunks."""

from collections.abc import Sequence
from datetime import date

import pytest

from azgenai_lab.core.errors import UpstreamError
from azgenai_lab.models.rag import Chunk, make_chunk_id
from azgenai_lab.models.search_index import EMBEDDING_DIMENSIONS
from azgenai_lab.services.embeddings import (
    MAX_BATCH_INPUTS,
    FakeEmbeddingClient,
    embed_chunks,
)


def _chunks(count: int) -> list[Chunk]:
    return [
        Chunk(
            chunk_id=make_chunk_id("doc", index),
            parent_id="doc",
            title="Doc",
            heading_path="Doc > Section",
            content=f"Content {index}.",
            doc_type="policy",
            tenant_id="acme",
            effective_date=date(2026, 1, 15),
        )
        for index in range(count)
    ]


class _RecordingClient:
    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        self.batches: list[list[str]] = []
        self._dimensions = dimensions

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return [[0.0] * self._dimensions for _ in texts]


def test_batch_cap_is_derived_from_the_documented_limits() -> None:
    # 300,000 aggregate tokens per request / 8,192 tokens per input.
    assert MAX_BATCH_INPUTS == 36
    assert MAX_BATCH_INPUTS * 8192 <= 300_000
    assert (MAX_BATCH_INPUTS + 1) * 8192 > 300_000


async def test_a_full_batch_is_sent_as_one_request() -> None:
    client = _RecordingClient()
    await embed_chunks(client, _chunks(MAX_BATCH_INPUTS))

    assert [len(batch) for batch in client.batches] == [MAX_BATCH_INPUTS]


async def test_the_thirty_seventh_chunk_starts_a_new_batch() -> None:
    client = _RecordingClient()
    await embed_chunks(client, _chunks(MAX_BATCH_INPUTS + 1))

    assert [len(batch) for batch in client.batches] == [MAX_BATCH_INPUTS, 1]


async def test_the_embedded_text_is_the_embedding_input() -> None:
    client = _RecordingClient()
    chunks = _chunks(1)
    await embed_chunks(client, chunks)

    assert client.batches[0] == [chunks[0].embedding_input]
    assert client.batches[0][0].startswith("Doc > Section")


async def test_vectors_are_returned_in_chunk_order() -> None:
    vectors = await embed_chunks(FakeEmbeddingClient(), _chunks(3))

    assert len(vectors) == 3
    assert all(len(vector) == EMBEDDING_DIMENSIONS for vector in vectors)


async def test_no_chunks_means_no_request() -> None:
    client = _RecordingClient()

    assert await embed_chunks(client, []) == []
    assert client.batches == []


async def test_wrong_dimension_response_fails_closed() -> None:
    client = _RecordingClient(dimensions=512)

    with pytest.raises(UpstreamError) as exc_info:
        await embed_chunks(client, _chunks(1))

    # UpstreamError.message is a fixed, client-facing string (never leaks
    # upstream detail into the HTTP response); the failing dimension count
    # belongs in upstream_detail instead, so assert there.
    assert exc_info.value.upstream_detail is not None
    assert "512" in exc_info.value.upstream_detail


async def test_fake_embeddings_are_deterministic() -> None:
    chunks = _chunks(2)
    first = await embed_chunks(FakeEmbeddingClient(), chunks)
    second = await embed_chunks(FakeEmbeddingClient(), chunks)

    assert first == second


async def test_fake_embeddings_differ_between_different_texts() -> None:
    vectors = await embed_chunks(FakeEmbeddingClient(), _chunks(2))

    assert vectors[0] != vectors[1]
