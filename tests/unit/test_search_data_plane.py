"""Everything the write path sends and receives over HTTP."""

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from azgenai_lab.core.config import Settings
from azgenai_lab.core.errors import ConfigurationError
from azgenai_lab.models.rag import IndexingAction
from azgenai_lab.services.azure_search import (
    SearchConfigurationError,
    SearchError,
    SearchRequestRejectedError,
    SearchUnavailableError,
)
from azgenai_lab.services.indexing_results import Disposition, classify
from azgenai_lab.services.search_data_plane import (
    ENUMERATION_PAGE_SIZE,
    IndexingBatch,
    SearchDataPlane,
    plan_batches,
)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        azure_search_endpoint="https://example.search.windows.net",
        azure_search_admin_key=SecretStr("k"),
        use_fake_search=False,
    )


def _plane(handler: object) -> SearchDataPlane:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return SearchDataPlane(_settings(), client=httpx.AsyncClient(transport=transport))


def _batch(body: bytes) -> IndexingBatch:
    """A batch carrying a body chosen by the test rather than by the planner.

    Response handling is what these tests are about, so the body is whatever
    the case needs; the tests that care what a real body looks like build one
    with `plan_batches()`.
    """
    return IndexingBatch(body, ("a",), IndexingAction.UPSERT)


async def test_create_or_update_index_puts_the_schema() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.read()
        return httpx.Response(201, json={})

    await _plane(handler).create_or_update_index()
    assert seen["method"] == "PUT"
    assert seen["url"] == (
        "https://example.search.windows.net/indexes/azgenai-lab-chunks"
        "?api-version=2026-04-01"
    )
    body = seen["body"]
    assert isinstance(body, bytes)
    assert b'"chunk-semantic"' in body and b'"content_vector"' in body


async def test_prefer_header_is_sent_only_on_the_index_put() -> None:
    # The REST contract marks `Prefer: return=representation` required on the
    # index PUT and nothing else. If it migrated into the shared header dict
    # for convenience, every other call would silently start sending it too.
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            seen["put_prefer"] = request.headers.get("prefer")
            return httpx.Response(201, json={})
        seen["post_prefer"] = request.headers.get("prefer")
        return httpx.Response(
            200, json={"value": [{"key": "a", "status": True, "statusCode": 200}]}
        )

    plane = _plane(handler)
    await plane.create_or_update_index()
    await plane.post_batch(_batch(b"{}"))

    assert seen["put_prefer"] == "return=representation"
    assert seen["post_prefer"] is None


async def test_extra_headers_replace_rather_than_duplicate_case_insensitively() -> None:
    # `_send`'s header merge must be case-insensitive: a future caller passing
    # `extra_headers={"Content-Type": ...}` should replace the shared
    # `content-type` entry, not add a second copy of it on the wire — HTTP
    # headers are case-insensitive but a plain dict merge is not.
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type_entries"] = [
            (name, value) for name, value in request.headers.raw if name.lower() == b"content-type"
        ]
        seen["get"] = request.headers.get("content-type")
        return httpx.Response(200, json={})

    plane = _plane(handler)
    await plane._send(
        "POST",
        "https://example.search.windows.net/x",
        extra_headers={"Content-Type": "text/plain"},
    )

    # Exactly one entry: a duplicate would show up as two raw header lines
    # and `.get()` would return the comma-joined value of both.
    assert seen["content_type_entries"] == [(b"content-type", b"text/plain")]
    assert seen["get"] == "text/plain"


async def test_create_or_update_index_maps_a_non_2xx_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "bad key"}})

    with pytest.raises(SearchConfigurationError):
        await _plane(handler).create_or_update_index()


async def test_delete_index_tolerates_a_missing_index() -> None:
    # Teardown must be idempotent: deleting what is already gone is success.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "no such index"}})

    await _plane(handler).delete_index()


async def test_delete_index_actually_issues_a_delete() -> None:
    # Ephemeral resources under a self-imposed monthly cap make a silent no-op
    # the failure mode that shows up as a bill: this pins that a call happens
    # at all, not just that a 404 from one is tolerated.
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(204)

    await _plane(handler).delete_index()
    assert seen["method"] == "DELETE"
    assert seen["url"] == (
        "https://example.search.windows.net/indexes/azgenai-lab-chunks"
        "?api-version=2026-04-01"
    )


async def test_post_batch_sends_the_exact_bytes_it_was_given() -> None:
    # The serialize-once regression, checked where it can actually fail: if a
    # caller handed the Python objects to `json=` instead of passing this
    # buffer through, the guarded size would not be the transmitted size. The
    # internal whitespace here does not survive a decode/re-encode round
    # trip, so this payload — unlike a compact one — actually pins that.
    body = b'{"value": [ {"chunk_id":"a","@search.action":"upload"} ]}'
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content"] = request.read()
        seen["content-type"] = request.headers.get("content-type")
        return httpx.Response(
            200, json={"value": [{"key": "a", "status": True, "statusCode": 200}]}
        )

    await _plane(handler).post_batch(_batch(body))
    assert seen["content"] == body
    assert seen["content-type"] == "application/json"


async def test_a_planned_batch_travels_as_the_bytes_it_was_measured_as() -> None:
    # Serialize-once spans two functions: `plan_batches()` measures a buffer
    # against the 16 MB ceiling, `post_batch()` sends it, and nothing in
    # between may re-encode. This checks the whole seam with a body the
    # planner really produced, non-ASCII included, because `ensure_ascii` is
    # the kind of setting a second encoder would not happen to share.
    #
    # Its limit is worth stating: a re-encode agreeing with the planner on
    # separators *and* `ensure_ascii` would slip through here, since the
    # transmitted bytes would coincide. That case is covered by the
    # whitespace-carrying body in the test above, whose spacing no encoder
    # reproduces.
    documents = [
        {"chunk_id": "doc-0000", "parent_id": "doc", "content": "café — refund"},
        {"chunk_id": "doc-0001", "parent_id": "doc", "content": "second"},
    ]
    batch = next(iter(plan_batches(documents, IndexingAction.UPSERT)))
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content"] = request.read()
        return httpx.Response(200, json={"value": []})

    await _plane(handler).post_batch(batch)

    assert seen["content"] == batch.body
    assert "café — refund".encode() in batch.body


async def test_post_batch_parses_a_207_document_by_document() -> None:
    # 207 is a *successful* HTTP response. A client that only checks whether
    # the call raised treats a partial failure as a complete one.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            207,
            json={
                "value": [
                    {"key": "a", "status": True, "statusCode": 200},
                    {"key": "b", "status": False, "statusCode": 503, "errorMessage": "busy"},
                    {"key": "c", "status": False, "statusCode": 400, "errorMessage": "bad"},
                ]
            },
        )

    results = await _plane(handler).post_batch(_batch(b"{}"))
    assert [r.key for r in results] == ["a", "b", "c"]
    assert classify(results[0]) is Disposition.SUCCEEDED
    assert classify(results[1]) is Disposition.RETRYABLE
    assert classify(results[2]) is Disposition.PERMANENT
    assert results[1].error_message == "busy"


@pytest.mark.parametrize(
    "payload",
    [
        {"value": "not-a-list"},
        {"value": [{"status": True, "statusCode": 200}]},          # no key
        {"value": [{"key": "a", "statusCode": 200}]},              # no status
        {"value": [{"key": "a", "status": True}]},                 # no statusCode
        {"value": [{"key": "a", "status": "yes", "statusCode": 200}]},
        {},
    ],
)
async def test_malformed_indexing_payloads_stay_inside_the_adapter(payload: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(SearchUnavailableError):
        await _plane(handler).post_batch(_batch(b"{}"))


@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_indexing_transient_statuses_are_unavailable(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "later"}})

    with pytest.raises(SearchUnavailableError):
        await _plane(handler).post_batch(_batch(b"{}"))


async def test_indexing_400_is_rejected_not_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad batch"}})

    with pytest.raises(SearchRequestRejectedError):
        await _plane(handler).post_batch(_batch(b"{}"))


async def test_indexing_404_is_a_configuration_error_not_a_search_error() -> None:
    # A missing index is a configuration failure, not a transient one:
    # `SearchConfigurationError` deliberately does not subclass `SearchError`,
    # so a retry coordinator written as `except SearchError:` will not retry
    # a call that can never succeed until the index is created.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "no such index"}})

    with pytest.raises(SearchConfigurationError) as caught:
        await _plane(handler).post_batch(_batch(b"{}"))

    assert not isinstance(caught.value, SearchError)


async def test_post_batch_rejects_a_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    with pytest.raises(SearchUnavailableError):
        await _plane(handler).post_batch(_batch(b"{}"))


async def test_indexing_connection_failure_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(SearchUnavailableError):
        await _plane(handler).post_batch(_batch(b"{}"))


async def test_an_unusable_endpoint_does_not_escape_as_an_httpx_error() -> None:
    # A soft hyphen pasted into the configured endpoint parses as a URL but
    # fails IDNA encoding when the request is sent, raising `httpx.InvalidURL`
    # — which does not derive from `httpx.HTTPError`. Without an explicit arm
    # for it, it would be the one way a raw transport type crosses this
    # boundary.
    settings = Settings(
        _env_file=None,
        azure_search_endpoint="https://exa\xadmple.search.windows.net",
        azure_search_admin_key=SecretStr("k"),
        use_fake_search=False,
    )

    # `httpx.URL(...)` raises `InvalidURL` at construction, before the
    # transport is ever consulted — this handler proves that: if it were
    # reached, the test would fail loudly here rather than pass vacuously
    # over a real DNS lookup and connection attempt.
    def unreachable_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("transport must not be reached: InvalidURL should raise first")

    plane = SearchDataPlane(
        settings, client=httpx.AsyncClient(transport=httpx.MockTransport(unreachable_handler))
    )

    with pytest.raises(SearchUnavailableError) as caught:
        await plane.post_batch(_batch(b"{}"))

    # Pin the cause, not just the class: a connection failure would also raise
    # `SearchUnavailableError`. If a future httpx normalised the soft hyphen
    # away instead of rejecting it, the host would resolve and this test would
    # otherwise keep passing — vacuously, and over a real network.
    assert isinstance(caught.value.__cause__, httpx.InvalidURL)


async def test_listing_chunk_ids_returns_them_in_response_order() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200, json={"value": [{"chunk_id": "doc-0001"}, {"chunk_id": "doc-0000"}]}
        )

    keys = await _plane(handler).list_chunk_ids("doc")
    assert keys == ["doc-0001", "doc-0000"]
    assert seen["url"] == (
        "https://example.search.windows.net/indexes/azgenai-lab-chunks"
        "/docs/search?api-version=2026-04-01"
    )


async def test_the_first_page_filters_on_the_parent_and_orders_by_the_key() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.read()))
        return httpx.Response(200, json={"value": []})

    await _plane(handler).list_chunk_ids("doc")
    assert seen["filter"] == "parent_id eq 'doc'"
    assert seen["orderby"] == "chunk_id asc"
    assert seen["search"] == "*"
    assert seen["select"] == "chunk_id"
    assert seen["top"] == ENUMERATION_PAGE_SIZE


async def test_resuming_uses_a_strict_greater_than_not_the_documented_ge() -> None:
    # `ge` — which the official paging example uses — repeats the previous
    # page's last key, and a caller that re-derives its cursor from that repeat
    # never advances: a silent infinite loop, not one duplicate row.
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.read()))
        return httpx.Response(200, json={"value": []})

    await _plane(handler).list_chunk_ids("doc", "doc-0001")
    assert seen["filter"] == "parent_id eq 'doc' and chunk_id gt 'doc-0001'"


@pytest.mark.parametrize(
    ("parent_id", "after", "expected"),
    [
        ("o'brien", None, "parent_id eq 'o''brien'"),
        ("doc", "o'brien-0001", "parent_id eq 'doc' and chunk_id gt 'o''brien-0001'"),
    ],
)
async def test_an_apostrophe_is_escaped_on_both_sides_of_the_expression(
    parent_id: str, after: str | None, expected: str
) -> None:
    # A single quote closes an OData string literal. Unescaped, a parent id or
    # a cursor carrying one turns a filter into a syntax error at best, and a
    # different filter — over a different document's chunks — at worst.
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.read()))
        return httpx.Response(200, json={"value": []})

    await _plane(handler).list_chunk_ids(parent_id, after)
    assert seen["filter"] == expected


async def test_listing_chunk_ids_rejects_a_result_without_a_chunk_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [{"parent_id": "doc"}]})

    with pytest.raises(SearchUnavailableError):
        await _plane(handler).list_chunk_ids("doc")


async def test_listing_chunk_ids_rejects_a_non_dict_result_without_leaking_it() -> None:
    # A bare entry in `value` (not an object at all) must not land whole in
    # `upstream_detail` — that string is log-destined, and the entry's own
    # text has no place there.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": ["not-a-document"]})

    with pytest.raises(SearchUnavailableError) as caught:
        await _plane(handler).list_chunk_ids("doc")

    detail = caught.value.upstream_detail
    assert detail is not None
    assert "not-a-document" not in detail


async def test_data_plane_requires_configuration() -> None:
    with pytest.raises(ConfigurationError):
        SearchDataPlane(Settings(_env_file=None, use_fake_search=False))
