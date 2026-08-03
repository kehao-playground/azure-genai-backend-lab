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
from azgenai_lab.models.principal import Principal
from azgenai_lab.models.rag import IndexingAction
from azgenai_lab.models.search import SearchMode
from azgenai_lab.models.search_index import EMBEDDING_DIMENSIONS
from azgenai_lab.services.azure_search import AzureSearchClient
from azgenai_lab.services.search_data_plane import SearchDataPlane, plan_batches

VECTOR = [0.1] * EMBEDDING_DIMENSIONS
_VECTOR_JSON = b",".join([b"0.1"] * EMBEDDING_DIMENSIONS)
_SELECT = b'"select":"chunk_id,parent_id,title,heading_path,content"'

# tenant "t1", no groups: build_acl_filter() renders this exact clause, which
# is what makes the byte-for-byte pins below reproducible.
PRINCIPAL = Principal(tenant_id="t1", user_id="u1", group_ids=())
_FILTER_JSON = b'"filter":"tenant_id eq \'t1\' and not allowed_groups/any()"'


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

    await client.search("refund window", mode=SearchMode.KEYWORD, top=5, principal=PRINCIPAL)

    assert recorder.bodies == [
        b'{"top":5,' + _SELECT + b',"search":"refund window",' + _FILTER_JSON + b"}"
    ]


async def test_vector_body_is_byte_for_byte_what_it_has_always_been() -> None:
    recorder = _Recorder()
    client = AzureSearchClient(_settings(), client=recorder.transport())

    await client.search(
        "refund window", VECTOR, mode=SearchMode.VECTOR, top=5, vector_k=3, principal=PRINCIPAL
    )

    assert recorder.bodies == [
        b'{"top":5,'
        + _SELECT
        + b',"vectorQueries":[{"kind":"vector","vector":['
        + _VECTOR_JSON
        + b'],"fields":"content_vector","k":3}],"vectorFilterMode":"preFilter",'
        + _FILTER_JSON
        + b"}"
    ]


async def test_hybrid_body_is_byte_for_byte_what_it_has_always_been() -> None:
    recorder = _Recorder()
    client = AzureSearchClient(_settings(), client=recorder.transport())

    await client.search(
        "refund window", VECTOR, mode=SearchMode.HYBRID, top=5, principal=PRINCIPAL
    )

    assert recorder.bodies == [
        b'{"top":5,'
        + _SELECT
        + b',"search":"refund window","vectorQueries":[{"kind":"vector","vector":['
        + _VECTOR_JSON
        + b'],"fields":"content_vector","k":50}],"vectorFilterMode":"preFilter",'
        + _FILTER_JSON
        + b"}"
    ]


async def test_hybrid_semantic_body_is_byte_for_byte_what_it_has_always_been() -> None:
    recorder = _Recorder()
    client = AzureSearchClient(_settings(), client=recorder.transport())

    acme_principal = Principal(tenant_id="acme", user_id="u1", group_ids=())
    await client.search(
        "refund window",
        VECTOR,
        mode=SearchMode.HYBRID_SEMANTIC,
        top=5,
        principal=acme_principal,
    )

    assert recorder.bodies == [
        b'{"top":5,'
        + _SELECT
        + b',"search":"refund window","vectorQueries":[{"kind":"vector","vector":['
        + _VECTOR_JSON
        + b'],"fields":"content_vector","k":50}],"vectorFilterMode":"preFilter",'
        b'"queryType":"semantic",'
        b'"semanticConfiguration":"chunk-semantic",'
        b'"filter":"tenant_id eq \'acme\' and not allowed_groups/any()"}'
    ]


async def test_indexing_bodies_are_byte_for_byte_what_they_have_always_been() -> None:
    # The indexing body is built by the planner and sent untouched, so the
    # bytes are pinned where they arrive: at the transport. Key order follows
    # the document's own insertion order with `@search.action` appended last,
    # separators are compact, and non-ASCII content travels as UTF-8 rather
    # than as `\\uXXXX` escapes — none of which any field-level assertion
    # elsewhere would notice changing.
    recorder = _Recorder()
    plane = SearchDataPlane(_settings(), client=recorder.transport())

    documents = [
        {"chunk_id": "doc-0000", "parent_id": "doc", "content": "café — refund"},
        {"chunk_id": "doc-0001", "parent_id": "doc", "content": "second"},
    ]
    for batch in plan_batches(documents, IndexingAction.UPSERT):
        await plane.post_batch(batch)
    for batch in plan_batches([{"chunk_id": "doc-0002"}], IndexingAction.REMOVE):
        await plane.post_batch(batch)

    assert recorder.bodies == [
        b'{"value":[{"chunk_id":"doc-0000","parent_id":"doc",'
        b'"content":"caf\xc3\xa9 \xe2\x80\x94 refund","@search.action":"upload"},'
        b'{"chunk_id":"doc-0001","parent_id":"doc","content":"second",'
        b'"@search.action":"upload"}]}',
        b'{"value":[{"chunk_id":"doc-0002","@search.action":"delete"}]}',
    ]


async def test_enumeration_bodies_are_byte_for_byte_what_they_have_always_been() -> None:
    # A parent id carrying an apostrophe, so the OData escaping is part of what
    # is pinned rather than something the fixture happens to avoid.
    recorder = _Recorder()
    plane = SearchDataPlane(_settings(), client=recorder.transport())

    await plane.list_chunk_ids("t", "o'brien")
    await plane.list_chunk_ids("t", "o'brien", "doc-0001")

    assert recorder.bodies == [
        b'{"search":"*","filter":"tenant_id eq \'t\' and parent_id eq \'o\'\'brien\'",'
        b'"select":"chunk_id","orderby":"chunk_id asc","top":1000}',
        b'{"search":"*","filter":"tenant_id eq \'t\' and parent_id eq \'o\'\'brien\' '
        b'and chunk_id gt \'doc-0001\'",'
        b'"select":"chunk_id","orderby":"chunk_id asc","top":1000}',
    ]
