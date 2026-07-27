"""The embedding boundary. Batching lives above the adapter so that a rejected
batch is attributable to a known set of chunks."""

import logging
from collections.abc import Sequence
from datetime import date

import httpx
import openai
import pytest
from openai.types import Embedding
from openai.types.create_embedding_response import CreateEmbeddingResponse, Usage

from azgenai_lab.core.config import Settings
from azgenai_lab.core.errors import ConfigurationError, UpstreamError, UpstreamServiceError
from azgenai_lab.models.rag import Chunk, make_chunk_id
from azgenai_lab.models.search_index import EMBEDDING_DIMENSIONS
from azgenai_lab.services.embeddings import (
    MAX_BATCH_INPUTS,
    AzureOpenAIEmbeddingClient,
    EmbeddingRejectedError,
    FakeEmbeddingClient,
    build_embedding_client,
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


def _status_error(
    error_cls: type[openai.APIStatusError],
    status_code: int,
    *,
    request_id: str | None = None,
) -> openai.APIStatusError:
    # Same construction the chat adapter's tests use: the SDK reads
    # response.request and response.status_code during __init__, so a real
    # httpx.Response is required — passing response=None raises AttributeError
    # before the code under test is reached.
    request = httpx.Request("POST", "https://example.openai.azure.com/openai/v1/embeddings")
    headers = {"x-request-id": request_id} if request_id is not None else None
    response = httpx.Response(status_code, request=request, headers=headers)
    return error_cls("boom", response=response, body=None)


class _RejectingClient:
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise EmbeddingRejectedError(upstream_detail="input too long", request_id="req-1")


class _RejectingOnSecondBatchClient:
    """The first batch succeeds; the second is rejected. Pins that a rejected
    batch's chunk ids are scoped to *that* batch, not to the whole call."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls == 2:
            raise EmbeddingRejectedError(upstream_detail="input too long", request_id="req-1")
        return [[0.0] * EMBEDDING_DIMENSIONS for _ in texts]


async def test_rejection_names_every_chunk_in_the_failed_batch() -> None:
    # 37 chunks split into a batch of MAX_BATCH_INPUTS (36) and a batch of 1;
    # only the second batch rejects. A call-scoped (rather than batch-scoped)
    # implementation would report all 37 chunk ids here instead of just the
    # failing batch's single chunk.
    chunks = _chunks(MAX_BATCH_INPUTS + 1)

    with pytest.raises(EmbeddingRejectedError) as excinfo:
        await embed_chunks(_RejectingOnSecondBatchClient(), chunks)

    assert excinfo.value.chunk_ids == (chunks[-1].chunk_id,)
    assert excinfo.value.request_id == "req-1"


async def test_rejection_logs_the_batch_for_manual_isolation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.ERROR, logger="azgenai_lab.services.embeddings"),
        pytest.raises(EmbeddingRejectedError),
    ):
        await embed_chunks(_RejectingClient(), _chunks(2))

    assert "doc-0000" in caplog.text
    assert "doc-0001" in caplog.text
    assert "req-1" in caplog.text


def test_rejection_is_not_retryable() -> None:
    error = EmbeddingRejectedError()

    assert error.code == "embedding_rejected"
    assert error.retryable is False
    # Indexing has no HTTP caller to blame; a rejected batch is our failure.
    assert error.status_code == 500


def test_composition_point_returns_the_fake_by_default() -> None:
    client = build_embedding_client(Settings(_env_file=None))

    assert isinstance(client, FakeEmbeddingClient)


def test_composition_point_requires_a_deployment_for_the_real_client() -> None:
    settings = Settings(
        _env_file=None,
        use_fake_embeddings=False,
        azure_openai_endpoint="https://example.openai.azure.com",
        azure_openai_api_key="secret",
    )

    # ConfigurationError.message is the fixed, client-facing string (never
    # leaks configuration detail into a response); the missing setting's name
    # belongs in upstream_detail instead, so assert there, not via `match=`.
    with pytest.raises(ConfigurationError) as excinfo:
        build_embedding_client(settings)

    assert excinfo.value.upstream_detail is not None
    assert "azure_openai_embedding_deployment" in excinfo.value.upstream_detail


def test_composition_point_builds_the_real_client_when_configured() -> None:
    settings = Settings(
        _env_file=None,
        use_fake_embeddings=False,
        azure_openai_endpoint="https://example.openai.azure.com",
        azure_openai_api_key="secret",
        azure_openai_embedding_deployment="embed-small",
    )

    assert isinstance(build_embedding_client(settings), AzureOpenAIEmbeddingClient)


async def test_bad_request_from_the_sdk_becomes_a_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        use_fake_embeddings=False,
        azure_openai_endpoint="https://example.openai.azure.com",
        azure_openai_api_key="secret",
        azure_openai_embedding_deployment="embed-small",
    )
    client = build_embedding_client(settings)

    async def _raise(**kwargs: object) -> None:
        raise _status_error(openai.BadRequestError, 400, request_id="req-abc")

    monkeypatch.setattr(client._client.embeddings, "create", _raise)  # type: ignore[attr-defined]

    with pytest.raises(EmbeddingRejectedError) as excinfo:
        await client.embed(["text"])

    assert excinfo.value.request_id == "req-abc"


async def test_an_unmapped_openai_error_does_not_escape_the_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # APIResponseValidationError inherits APIError -> OpenAIError but is
    # neither an APIStatusError nor an APIConnectionError, so it exercises
    # the catch-all: it's raised when a 2xx response body fails validation
    # against CreateEmbeddingResponse (e.g. a gateway mangling the body, or
    # an upstream shape change). It must come out as an UpstreamError
    # subclass, not escape raw past this boundary.
    settings = Settings(
        _env_file=None,
        use_fake_embeddings=False,
        azure_openai_endpoint="https://example.openai.azure.com",
        azure_openai_api_key="secret",
        azure_openai_embedding_deployment="embed-small",
    )
    client = build_embedding_client(settings)

    request = httpx.Request("POST", "https://example.openai.azure.com/openai/v1/embeddings")
    response = httpx.Response(200, request=request)

    async def _raise(**kwargs: object) -> None:
        raise openai.APIResponseValidationError(response=response, body=None)

    monkeypatch.setattr(client._client.embeddings, "create", _raise)  # type: ignore[attr-defined]

    with pytest.raises(UpstreamServiceError):
        await client.embed(["text"])


async def test_the_real_adapter_returns_vectors_on_success(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(
        _env_file=None,
        use_fake_embeddings=False,
        azure_openai_endpoint="https://example.openai.azure.com",
        azure_openai_api_key="secret",
        azure_openai_embedding_deployment="embed-small",
    )
    client = build_embedding_client(settings)

    vectors = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    response = CreateEmbeddingResponse(
        data=[
            Embedding(embedding=vectors[0], index=0, object="embedding"),
            Embedding(embedding=vectors[1], index=1, object="embedding"),
        ],
        model="embed-small",
        object="list",
        usage=Usage(prompt_tokens=12, total_tokens=12),
    )

    async def _create(**kwargs: object) -> CreateEmbeddingResponse:
        return response

    monkeypatch.setattr(client._client.embeddings, "create", _create)  # type: ignore[attr-defined]

    with caplog.at_level(logging.INFO, logger="azgenai_lab.services.embeddings"):
        result = await client.embed(["one", "two"])

    assert result == vectors
    # Pins the INFO line's field names against a future SDK rename, and
    # confirms no secret (the API key) leaks into the log line.
    assert "embed-small" in caplog.text
    assert "inputs=2" in caplog.text
    assert "prompt_tokens=12" in caplog.text
    assert "total_tokens=12" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.parametrize(
    ("error_cls", "status_code"),
    [
        (openai.AuthenticationError, 401),
        (openai.PermissionDeniedError, 403),
        (openai.NotFoundError, 404),
    ],
)
async def test_credential_and_deployment_failures_are_configuration_errors(
    monkeypatch: pytest.MonkeyPatch,
    error_cls: type[openai.APIStatusError],
    status_code: int,
) -> None:
    # A bad key or a wrong deployment name is our misconfiguration. Routing it
    # to EmbeddingRejectedError would blame the corpus for an operator mistake.
    settings = Settings(
        _env_file=None,
        use_fake_embeddings=False,
        azure_openai_endpoint="https://example.openai.azure.com",
        azure_openai_api_key="secret",
        azure_openai_embedding_deployment="embed-small",
    )
    client = build_embedding_client(settings)

    async def _raise(**kwargs: object) -> None:
        raise _status_error(error_cls, status_code)

    monkeypatch.setattr(client._client.embeddings, "create", _raise)  # type: ignore[attr-defined]

    with pytest.raises(ConfigurationError):
        await client.embed(["text"])
