"""The exact bytes each search request puts on the wire.

Field-by-field assertions elsewhere prove that the right keys are present.
They cannot catch a change in key *order*, in separator style, or in how a
float is rendered — all of which change the request without changing any
assertion about its contents. This file pins the serialized body itself, so
any reshaping of where request construction lives has to leave the wire
untouched.

The literals are written out in full rather than rebuilt from the modules
under test: an expectation derived from the code it checks agrees with that
code by construction.
"""

import httpx
from pydantic import SecretStr

from azgenai_lab.core.config import Settings
from azgenai_lab.models.search import SearchMode
from azgenai_lab.models.search_index import EMBEDDING_DIMENSIONS
from azgenai_lab.services.azure_search import AzureSearchClient
from azgenai_lab.services.search_data_plane import SearchDataPlane

VECTOR = [0.1] * EMBEDDING_DIMENSIONS
_VECTOR_JSON = b",".join([b"0.1"] * EMBEDDING_DIMENSIONS)
_SELECT = b'"select":"chunk_id,parent_id,title,heading_path,content"'


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        azure_search_endpoint="https://example.search.windows.net",
        azure_search_admin_key=SecretStr("k"),
        use_fake_search=False,
    )


class _Recorder:
    """A transport that keeps every request body and answers with nothing."""

    def __init__(self) -> None:
        self.bodies: list[bytes] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(request.read())
        return httpx.Response(200, json={"value": []})

    def transport(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


async def test_keyword_body_is_byte_for_byte_what_it_has_always_been() -> None:
    recorder = _Recorder()
    client = AzureSearchClient(_settings(), client=recorder.transport())

    await client.search("refund window", mode=SearchMode.KEYWORD, top=5)

    assert recorder.bodies == [
        b'{"top":5,' + _SELECT + b',"search":"refund window"}'
    ]


async def test_vector_body_is_byte_for_byte_what_it_has_always_been() -> None:
    recorder = _Recorder()
    client = AzureSearchClient(_settings(), client=recorder.transport())

    await client.search("refund window", VECTOR, mode=SearchMode.VECTOR, top=5, vector_k=3)

    assert recorder.bodies == [
        b'{"top":5,'
        + _SELECT
        + b',"vectorQueries":[{"kind":"vector","vector":['
        + _VECTOR_JSON
        + b'],"fields":"content_vector","k":3}]}'
    ]


async def test_hybrid_body_is_byte_for_byte_what_it_has_always_been() -> None:
    recorder = _Recorder()
    client = AzureSearchClient(_settings(), client=recorder.transport())

    await client.search("refund window", VECTOR, mode=SearchMode.HYBRID, top=5)

    assert recorder.bodies == [
        b'{"top":5,'
        + _SELECT
        + b',"search":"refund window","vectorQueries":[{"kind":"vector","vector":['
        + _VECTOR_JSON
        + b'],"fields":"content_vector","k":50}]}'
    ]


async def test_hybrid_semantic_body_is_byte_for_byte_what_it_has_always_been() -> None:
    recorder = _Recorder()
    client = AzureSearchClient(_settings(), client=recorder.transport())

    await client.search(
        "refund window",
        VECTOR,
        mode=SearchMode.HYBRID_SEMANTIC,
        top=5,
        filter="tenant_id eq 'acme'",
    )

    assert recorder.bodies == [
        b'{"top":5,'
        + _SELECT
        + b',"search":"refund window","vectorQueries":[{"kind":"vector","vector":['
        + _VECTOR_JSON
        + b'],"fields":"content_vector","k":50}],"queryType":"semantic",'
        b'"semanticConfiguration":"chunk-semantic","filter":"tenant_id eq \'acme\'"}'
    ]


async def test_enumeration_bodies_are_byte_for_byte_what_they_have_always_been() -> None:
    # A parent id carrying an apostrophe, so the OData escaping is part of what
    # is pinned rather than something the fixture happens to avoid.
    recorder = _Recorder()
    plane = SearchDataPlane(_settings(), client=recorder.transport())

    await plane.list_chunk_ids("o'brien")
    await plane.list_chunk_ids("o'brien", "doc-0001")

    assert recorder.bodies == [
        b'{"search":"*","filter":"parent_id eq \'o\'\'brien\'",'
        b'"select":"chunk_id","orderby":"chunk_id asc","top":1000}',
        b'{"search":"*","filter":"parent_id eq \'o\'\'brien\' '
        b'and chunk_id gt \'doc-0001\'",'
        b'"select":"chunk_id","orderby":"chunk_id asc","top":1000}',
    ]
