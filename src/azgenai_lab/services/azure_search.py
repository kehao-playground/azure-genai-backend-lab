"""The Azure AI Search boundary (query side).

Raw REST rather than the SDK: `to_index_definition()` already emits the wire
body, `httpx` is already a dependency, and the SDK would layer a second
vocabulary over a contract this project owns. The cost is that URL shapes,
headers and `@search.*` field names live here — and nowhere else.

Search failures get their own vocabulary. A query-time 422 means our request
shape is wrong; a 422 inside an indexing 207 means one document is retryable.
Same number, opposite responses, so the two never share a classifier.
"""

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from azgenai_lab.core.config import Settings
from azgenai_lab.core.errors import ConfigurationError, UpstreamError
from azgenai_lab.models.search import (
    DEFAULT_VECTOR_K,
    SearchHit,
    SearchMode,
    SearchResult,
    build_search_body,
)
from azgenai_lab.models.search_index import INDEX_NAME, SEARCH_API_VERSION

logger = logging.getLogger(__name__)

_CONFIGURATION_STATUSES = frozenset({401, 403, 404})
_REJECTED_STATUSES = frozenset({400, 422})


class _Diagnosable:
    """Mixin carrying the two fields that only ever reach the log."""

    def __init__(
        self,
        upstream_detail: str | None = None,
        *,
        status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(upstream_detail)  # type: ignore[call-arg]
        self.status = status
        self.request_id = request_id


class SearchError(_Diagnosable, UpstreamError):
    """Base for query-side search failures that are not configuration.

    Configuration failures (bad key, missing role assignment, missing index)
    deliberately route through ``SearchConfigurationError`` /
    ``ConfigurationError`` instead of through this class — ``except
    SearchError:`` will not catch a 401/403/404. ``UpstreamError`` is the true
    catch-all across both branches.

    ``status`` and ``request_id`` are for the log. The client-facing
    ``message`` is fixed per subclass and never carries the endpoint, the key,
    the filter or the query text.
    """


class SearchConfigurationError(_Diagnosable, ConfigurationError):
    """Bad key, missing role assignment, or an index that does not exist.

    Still a ``ConfigurationError`` — the client contract and status code are
    unchanged — but it keeps the status and request id the shared class has no
    room for, because a 404 here is diagnosed very differently from a 403.
    """


class SearchRequestRejectedError(SearchError):
    """The service rejected the query itself — our request shape is wrong.

    A 500 rather than a 400: retrieval is issued by this application, so a
    malformed search request is our bug, not the caller's.
    """

    status_code = 500
    code = "search_request_rejected"
    message = "The search request was rejected by the search service."


class SearchUnavailableError(SearchError):
    status_code = 503
    code = "search_unavailable"
    message = "The search service is unavailable; retry later."


@dataclass(frozen=True)
class SearchDiagnostics:
    """Everything the live-session evidence file must record about one call.

    Kept off ``SearchResult`` because it is transport detail, not domain data:
    the Protocol's contract stays free of HTTP.
    """

    request_body: dict[str, Any]
    # None when the request never produced a response at all (connection
    # refused, DNS failure, timeout). That is a distinct outcome from any HTTP
    # status and must not be reported as one.
    status: int | None
    request_id: str | None
    latency_ms: float


class SearchClient(Protocol):
    async def search(
        self,
        query_text: str,
        query_vector: Sequence[float] | None = None,
        *,
        mode: SearchMode = SearchMode.HYBRID,
        top: int,
        filter: str | None = None,
        vector_k: int = DEFAULT_VECTOR_K,
    ) -> SearchResult: ...


def search_url(endpoint: str) -> str:
    return (
        f"{endpoint.rstrip('/')}/indexes/{INDEX_NAME}/docs/search"
        f"?api-version={SEARCH_API_VERSION}"
    )


def _number(value: object, field: str) -> float:
    """Convert a score, or fail inside the adapter rather than at the caller."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SearchUnavailableError(f"{field} was {value!r}, which is not a number")
    return float(value)


def _text(document: dict[str, Any], field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str):
        raise SearchUnavailableError(
            f"search result field {field} was {value!r}, expected a string"
        )
    return value


def parse_hits(payload: object) -> tuple[SearchHit, ...]:
    """Turn a 2xx body into hits, or raise if it is not the shape we expect.

    Every field is checked. A missing ``chunk_id`` silently becoming ``""``
    would produce a citation pointing at nothing, which is worse than a loud
    failure.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
        raise SearchUnavailableError("search response has no 'value' array")
    hits: list[SearchHit] = []
    for document in payload["value"]:
        if not isinstance(document, dict):
            raise SearchUnavailableError(f"search result was {document!r}, expected an object")
        if "@search.score" not in document:
            raise SearchUnavailableError("search result is missing @search.score")
        # Membership, not truthiness: 0.0 is a real reranker verdict.
        raw_reranker = document.get("@search.rerankerScore")
        hits.append(
            SearchHit(
                chunk_id=_text(document, "chunk_id"),
                parent_id=_text(document, "parent_id"),
                title=_text(document, "title"),
                heading_path=_text(document, "heading_path"),
                content=_text(document, "content"),
                score=_number(document["@search.score"], "@search.score"),
                reranker_score=(
                    None
                    if raw_reranker is None
                    else _number(raw_reranker, "@search.rerankerScore")
                ),
            )
        )
    return tuple(hits)


def map_search_status(status: int, detail: str, request_id: str | None) -> UpstreamError:
    if status in _CONFIGURATION_STATUSES:
        return SearchConfigurationError(
            f"search returned {status}: {detail}", status=status, request_id=request_id
        )
    if status in _REJECTED_STATUSES:
        return SearchRequestRejectedError(detail, status=status, request_id=request_id)
    return SearchUnavailableError(detail, status=status, request_id=request_id)


class AzureSearchClient:
    """Query adapter over the stable data-plane REST API."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        if not settings.azure_search_endpoint or not settings.azure_search_admin_key:
            raise ConfigurationError(
                "azure_search_endpoint and azure_search_admin_key are required "
                "when use_fake_search is false"
            )
        self._url = search_url(settings.azure_search_endpoint)
        self._headers = {
            "api-key": settings.azure_search_admin_key.get_secret_value(),
            "content-type": "application/json",
        }
        self._client = client or httpx.AsyncClient(timeout=settings.llm_timeout_seconds)
        # One record rather than four mutable fields a caller has to read in
        # the right order and narrow individually. The live evidence file
        # needs all of it or none of it. ``request_body`` is stored as a
        # shallow copy (see the construction sites below), so a caller cannot
        # rebind a top-level key, but nested values — the ``vectorQueries``
        # entry in particular — are still shared with the dict that went to
        # the transport. Deep-copying a 1536-float vector on every call is not
        # worth the protection.
        self.last_diagnostics: SearchDiagnostics | None = None

    async def search(
        self,
        query_text: str,
        query_vector: Sequence[float] | None = None,
        *,
        mode: SearchMode = SearchMode.HYBRID,
        top: int,
        filter: str | None = None,
        vector_k: int = DEFAULT_VECTOR_K,
    ) -> SearchResult:
        # Cleared first, before argument validation can raise. Leaving the
        # previous call's record in place would let a rejected or failed call
        # be written up with the *last successful* call's body, status and
        # request id — misattributed evidence that no checksum on the file
        # can detect, because the file is intact and the contents are simply
        # about the wrong request.
        self.last_diagnostics = None
        body = build_search_body(
            query_text, query_vector, mode=mode, top=top, filter=filter, vector_k=vector_k
        )
        started = time.perf_counter()
        try:
            response = await self._client.post(self._url, json=body, headers=self._headers)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # No response exists, so there is no status and no request id —
            # recorded as None rather than omitted, because "the call never
            # landed" is itself the finding.
            self.last_diagnostics = SearchDiagnostics(
                request_body=dict(body),
                status=None,
                request_id=None,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
            raise SearchUnavailableError(str(exc)) from exc
        # Recorded even on the failure paths below: an evidence file that only
        # captures successful calls cannot rule out a configuration error.
        request_id = response.headers.get("request-id")
        self.last_diagnostics = SearchDiagnostics(
            request_body=dict(body),
            status=response.status_code,
            request_id=request_id,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

        if response.status_code >= 400:
            raise map_search_status(response.status_code, response.text[:500], request_id)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SearchUnavailableError(
                f"search response body was not JSON: {exc}", request_id=request_id
            ) from exc
        hits = parse_hits(payload)

        # Per-stage retrieval logging: which stage was asked, how wide the
        # candidate window was, what came back and in what order. Day 8's
        # correlation id joins these to the prompt and usage lines.
        # Read from the body that was actually built, not re-derived from the
        # caller's arguments: a future mode that sends no vector would make a
        # re-derived rule report a `k` that was never on the wire.
        effective_k = vector_k if "vectorQueries" in body else None
        logger.info(
            "search mode=%s vector_k=%s top=%d returned=%d latency_ms=%.1f "
            "chunk_ids=%s scores=%s reranker_scores=%s",
            mode.value,
            effective_k,
            top,
            len(hits),
            self.last_diagnostics.latency_ms,
            ",".join(hit.chunk_id for hit in hits),
            ",".join(f"{hit.score:.6f}" for hit in hits),
            ",".join("-" if h.reranker_score is None else f"{h.reranker_score:.3f}" for h in hits),
        )
        return SearchResult(hits=hits, mode=mode, vector_k=effective_k)
