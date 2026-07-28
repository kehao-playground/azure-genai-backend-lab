"""The adapter owns every REST detail. These drive the real adapter through a
MockTransport — a fake cannot prove HTTP error mapping."""

import httpx
import pytest
from pydantic import SecretStr

from azgenai_lab.core.config import Settings
from azgenai_lab.core.errors import ConfigurationError
from azgenai_lab.models.search import SearchMode
from azgenai_lab.models.search_index import EMBEDDING_DIMENSIONS
from azgenai_lab.services.azure_search import (
    AzureSearchClient,
    SearchConfigurationError,
    SearchRequestRejectedError,
    SearchUnavailableError,
)

VECTOR = [0.1] * EMBEDDING_DIMENSIONS


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        azure_search_endpoint="https://example.search.windows.net",
        azure_search_admin_key=SecretStr("k"),
        use_fake_search=False,
    )


def _client(handler: object) -> AzureSearchClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return AzureSearchClient(_settings(), client=httpx.AsyncClient(transport=transport))


def _hit(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "@search.score": 1.5,
        "chunk_id": "returns-policy-0001",
        "parent_id": "returns-policy",
        "title": "Returns Policy",
        "heading_path": "Returns Policy > Refund window",
        "content": "Customers may return most items within 30 days.",
    }
    document.update(overrides)
    return document


async def test_request_targets_the_right_url_and_carries_the_key() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("api-key")
        return httpx.Response(200, json={"value": [_hit()]})

    await _client(handler).search("q", mode=SearchMode.KEYWORD, top=5)
    assert seen["url"] == (
        "https://example.search.windows.net/indexes/azgenai-lab-chunks"
        "/docs/search?api-version=2026-04-01"
    )
    assert seen["key"] == "k"


async def test_parses_both_scores() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [_hit(**{"@search.rerankerScore": 3.25})]})

    result = await _client(handler).search("q", VECTOR, mode=SearchMode.HYBRID_SEMANTIC, top=5)
    assert result.hits[0].score == 1.5
    assert result.hits[0].reranker_score == 3.25
    assert result.mode is SearchMode.HYBRID_SEMANTIC
    assert result.vector_k == 50


async def test_a_zero_reranker_score_is_a_verdict_not_a_missing_value() -> None:
    # `payload.get(...) or None` would turn an explicit "irrelevant" into
    # "not ranked" — opposite signals for Day 14's no-answer policy.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [_hit(**{"@search.rerankerScore": 0.0})]})

    result = await _client(handler).search("q", VECTOR, mode=SearchMode.HYBRID_SEMANTIC, top=5)
    assert result.hits[0].reranker_score == 0.0


async def test_absent_reranker_score_is_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [_hit()]})

    result = await _client(handler).search("q", mode=SearchMode.KEYWORD, top=5)
    assert result.hits[0].reranker_score is None
    # The other direction of the same contract: a mode that puts no
    # `vectorQueries` on the wire must not report a `k` that was never sent.
    assert result.vector_k is None


async def test_empty_result_set_is_not_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": []})

    assert (await _client(handler).search("q", mode=SearchMode.KEYWORD, top=5)).hits == ()


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_auth_and_missing_index_are_configuration_errors(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, json={"error": {"message": "nope"}}, headers={"request-id": "r1"}
        )

    with pytest.raises(SearchConfigurationError) as caught:
        await _client(handler).search("q", mode=SearchMode.KEYWORD, top=5)
    # Spec §2.10 requires status and request id to survive for the log even on
    # the configuration branch.
    assert caught.value.status == status
    assert caught.value.request_id == "r1"
    assert isinstance(caught.value, ConfigurationError)


@pytest.mark.parametrize("status", [400, 422])
async def test_bad_query_shape_is_rejected(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "bad"}})

    with pytest.raises(SearchRequestRejectedError):
        await _client(handler).search("q", mode=SearchMode.KEYWORD, top=5)


@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_transient_statuses_are_unavailable(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "later"}})

    with pytest.raises(SearchUnavailableError):
        await _client(handler).search("q", mode=SearchMode.KEYWORD, top=5)


async def test_connection_failure_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(SearchUnavailableError):
        await _client(handler).search("q", mode=SearchMode.KEYWORD, top=5)


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

    client = AzureSearchClient(
        settings, client=httpx.AsyncClient(transport=httpx.MockTransport(unreachable_handler))
    )

    with pytest.raises(SearchUnavailableError) as caught:
        await client.search("q", mode=SearchMode.KEYWORD, top=5)

    # Pin the cause, not just the class: a connection failure would also raise
    # `SearchUnavailableError`. If a future httpx normalised the soft hyphen
    # away instead of rejecting it, the host would resolve and this test would
    # otherwise keep passing — vacuously, and over a real network.
    assert isinstance(caught.value.__cause__, httpx.InvalidURL)

    diagnostics = client.last_diagnostics
    assert diagnostics is not None
    assert diagnostics.status is None


@pytest.mark.parametrize(
    "payload",
    [
        {"value": [{"chunk_id": "a"}]},                       # no score
        {"value": [_hit(**{"@search.score": None})]},          # null score
        {"value": [_hit(**{"@search.score": "high"})]},        # non-numeric score
        {"value": [_hit(**{"@search.rerankerScore": "n/a"})]}, # non-numeric reranker
        {"value": [{"@search.score": 1.0}]},                   # no chunk_id
        {"value": [5]},                                        # hit is not an object
        {"value": "not-a-list"},
        {"nothing": True},
        [1, 2, 3],
    ],
)
async def test_every_malformed_2xx_payload_stays_inside_the_adapter(payload: object) -> None:
    # Nothing transport- or shape-specific may escape this boundary: a raw
    # TypeError from float() is exactly the leak this test exists to stop.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(SearchUnavailableError):
        await _client(handler).search("q", VECTOR, mode=SearchMode.HYBRID_SEMANTIC, top=5)


async def test_non_object_hit_is_reported_distinctly_from_a_missing_score() -> None:
    # A hit that is not an object at all has no @search.score to be missing;
    # reporting it as "missing @search.score" would misdescribe the payload.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [5]})

    with pytest.raises(SearchUnavailableError) as caught:
        await _client(handler).search("q", mode=SearchMode.KEYWORD, top=5)
    assert caught.value.upstream_detail is not None
    assert "expected an object" in caught.value.upstream_detail


async def test_non_json_2xx_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    with pytest.raises(SearchUnavailableError):
        await _client(handler).search("q", mode=SearchMode.KEYWORD, top=5)


async def test_client_facing_message_does_not_leak_endpoint_key_filter_or_query_text() -> None:
    # Every forbidden string must actually be reachable from the message
    # under test, or the assertions pass regardless of whether the adapter
    # leaks anything. The handler below echoes the request URL, the api-key
    # header and the request body (which carries the query text and the
    # filter) back in its 400 detail, so a regression that forwards upstream
    # detail into the client-facing message has something real to catch.
    admin_key = "very-secret-admin-key-4b2d9c"
    query_text = "distinctive-query-text-9f3c"
    filter_expression = "distinctive-filter-a71e"

    def handler(request: httpx.Request) -> httpx.Response:
        detail = (
            f"bad request to {request.url} "
            f"with api-key {request.headers.get('api-key')} "
            f"body {request.content.decode()}"
        )
        return httpx.Response(400, json={"error": {"message": detail}})

    transport = httpx.MockTransport(handler)
    settings = Settings(
        _env_file=None,
        azure_search_endpoint="https://example.search.windows.net",
        azure_search_admin_key=SecretStr(admin_key),
        use_fake_search=False,
    )
    client = AzureSearchClient(settings, client=httpx.AsyncClient(transport=transport))

    with pytest.raises(SearchRequestRejectedError) as caught:
        await client.search(
            query_text, mode=SearchMode.KEYWORD, top=5, filter=filter_expression
        )

    # UpstreamError.__init__ passes self.message to Exception.__init__, so
    # str(exc) is a second caller-reachable surface — both must be clean.
    for forbidden in ("example.search.windows.net", admin_key, query_text, filter_expression):
        assert forbidden not in caught.value.message
        assert forbidden not in str(caught.value)


async def test_diagnostics_are_recorded_on_success_and_on_failure() -> None:
    # The evidence file must be able to rule out a configuration error, which
    # means the failing calls are exactly the ones whose status and request id
    # matter most.
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [_hit()]}, headers={"request-id": "ok-1"})

    client = _client(ok)
    await client.search("q", mode=SearchMode.KEYWORD, top=5)
    assert client.last_diagnostics is not None
    assert client.last_diagnostics.status == 200
    assert client.last_diagnostics.request_id == "ok-1"
    assert client.last_diagnostics.latency_ms >= 0
    assert client.last_diagnostics.request_body["search"] == "q"

    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"message": "bad"}}, headers={"request-id": "e-1"}
        )

    failing = _client(bad)
    with pytest.raises(SearchRequestRejectedError):
        await failing.search("q", mode=SearchMode.KEYWORD, top=5)
    assert failing.last_diagnostics is not None
    assert failing.last_diagnostics.status == 400
    assert failing.last_diagnostics.request_id == "e-1"


async def test_a_transport_failure_never_inherits_the_previous_call_diagnostics() -> None:
    # Sequential, single-threaded, no concurrency involved: call one succeeds,
    # call two never reaches the service. If diagnostics were only written on
    # response, call two would be written up with call one's body, its 200 and
    # its request id — evidence attributed to the wrong request, which a
    # checksum over the file cannot detect.
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                200, json={"value": [_hit()]}, headers={"request-id": "first"}
            )
        raise httpx.ConnectError("no route")

    client = _client(handler)
    await client.search("first query", mode=SearchMode.KEYWORD, top=5)
    assert client.last_diagnostics is not None
    assert client.last_diagnostics.status == 200

    with pytest.raises(SearchUnavailableError):
        await client.search("second query", VECTOR, mode=SearchMode.VECTOR, top=5)

    diagnostics = client.last_diagnostics
    assert diagnostics is not None
    assert diagnostics.status is None, "a call that never landed has no HTTP status"
    assert diagnostics.request_id is None
    assert "vectorQueries" in diagnostics.request_body, "body must be the failed call's"
    assert diagnostics.request_body.get("search") is None


async def test_a_call_rejected_by_argument_validation_leaves_no_stale_diagnostics() -> None:
    # validate_search_arguments() raises before any HTTP request is built, so
    # this call never reaches the try/except that records diagnostics either
    # way. If the previous call's record were still in place, it would be
    # mistaken for evidence about *this* rejected call, not the one that
    # actually produced it.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [_hit()]}, headers={"request-id": "first"})

    client = _client(handler)
    await client.search("first query", mode=SearchMode.KEYWORD, top=5)
    assert client.last_diagnostics is not None

    with pytest.raises(ValueError):
        # A vector of the wrong width fails validate_search_arguments().
        await client.search("second query", [0.1], mode=SearchMode.VECTOR, top=5)

    assert client.last_diagnostics is None


async def test_missing_configuration_fails_fast() -> None:
    with pytest.raises(ConfigurationError):
        AzureSearchClient(Settings(_env_file=None, use_fake_search=False))
