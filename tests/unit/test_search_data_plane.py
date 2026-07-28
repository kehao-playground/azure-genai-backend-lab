"""Everything the write path sends and receives over HTTP."""

import httpx
import pytest
from pydantic import SecretStr

from azgenai_lab.core.config import Settings
from azgenai_lab.core.errors import ConfigurationError
from azgenai_lab.services.azure_search import (
    SearchRequestRejectedError,
    SearchUnavailableError,
)
from azgenai_lab.services.indexing_results import Disposition, classify
from azgenai_lab.services.search_data_plane import SearchDataPlane


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
    await plane.post_index(b"{}")

    assert seen["put_prefer"] == "return=representation"
    assert seen["post_prefer"] is None


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


async def test_post_index_sends_the_exact_bytes_it_was_given() -> None:
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

    await _plane(handler).post_index(body)
    assert seen["content"] == body
    assert seen["content-type"] == "application/json"


async def test_post_index_parses_a_207_document_by_document() -> None:
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

    results = await _plane(handler).post_index(b"{}")
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
        await _plane(handler).post_index(b"{}")


@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_indexing_transient_statuses_are_unavailable(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "later"}})

    with pytest.raises(SearchUnavailableError):
        await _plane(handler).post_index(b"{}")


async def test_indexing_400_is_rejected_not_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad batch"}})

    with pytest.raises(SearchRequestRejectedError):
        await _plane(handler).post_index(b"{}")


async def test_indexing_connection_failure_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(SearchUnavailableError):
        await _plane(handler).post_index(b"{}")


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
    plane = SearchDataPlane(settings, client=httpx.AsyncClient())

    with pytest.raises(SearchUnavailableError) as caught:
        await plane.post_index(b"{}")

    # Pin the cause, not just the class: a connection failure would also raise
    # `SearchUnavailableError`. If a future httpx normalised the soft hyphen
    # away instead of rejecting it, the host would resolve and this test would
    # otherwise keep passing — vacuously, and over a real network.
    assert isinstance(caught.value.__cause__, httpx.InvalidURL)


async def test_post_search_returns_chunk_ids_in_response_order() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200, json={"value": [{"chunk_id": "doc-0001"}, {"chunk_id": "doc-0000"}]}
        )

    keys = await _plane(handler).post_search({"search": "*", "top": 1000})
    assert keys == ["doc-0001", "doc-0000"]
    assert seen["url"] == (
        "https://example.search.windows.net/indexes/azgenai-lab-chunks"
        "/docs/search?api-version=2026-04-01"
    )


async def test_post_search_rejects_a_result_without_a_chunk_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [{"parent_id": "doc"}]})

    with pytest.raises(SearchUnavailableError):
        await _plane(handler).post_search({"search": "*"})


async def test_data_plane_requires_configuration() -> None:
    with pytest.raises(ConfigurationError):
        SearchDataPlane(Settings(_env_file=None, use_fake_search=False))
