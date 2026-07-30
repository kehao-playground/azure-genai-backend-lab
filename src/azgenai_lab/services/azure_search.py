"""The Azure AI Search boundary (query side).

Raw REST rather than the SDK: `to_index_definition()` already emits the wire
body, `httpx` is already a dependency, and the SDK would layer a second
vocabulary over a contract this project owns. The cost is that URL shapes,
headers, request JSON and `@search.*` field names live here — and nowhere
else. `models/search.py` names the modes and the hit; it does not know that a
vector query is spelled `vectorQueries`, which is what keeps swapping this
adapter for the SDK a one-file change.

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
    TEXT_QUERY_MODES,
    VECTOR_MODES,
    SearchHit,
    SearchMode,
    SearchResult,
    validate_search_arguments,
)
from azgenai_lab.models.search_index import (
    INDEX_NAME,
    SEARCH_API_VERSION,
    SEMANTIC_CONFIGURATION_NAME,
    VECTOR_FIELD,
)

logger = logging.getLogger(__name__)

_CONFIGURATION_STATUSES = frozenset({401, 403, 404})
_REJECTED_STATUSES = frozenset({400, 422})

# Vectors are never selected back: not human-readable, large, and the field is
# not retrievable anyway.
SELECT_FIELDS = ("chunk_id", "parent_id", "title", "heading_path", "content")


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

    async def aclose(self) -> None: ...


def search_url(endpoint: str) -> str:
    return (
        f"{endpoint.rstrip('/')}/indexes/{INDEX_NAME}/docs/search"
        f"?api-version={SEARCH_API_VERSION}"
    )


def build_search_body(
    query_text: str,
    query_vector: Sequence[float] | None = None,
    *,
    mode: SearchMode,
    top: int,
    filter: str | None = None,
    vector_k: int = DEFAULT_VECTOR_K,
) -> dict[str, Any]:
    """Build the Search Documents request body for one mode.

    Module-level rather than a method so the four request shapes — the thing
    the article is about — can be read and asserted without a client, an
    endpoint or a key. It stays inside this module because every name in it
    (``vectorQueries``, ``queryType``, ``semanticConfiguration``) is Azure's
    vocabulary, not this project's.

    ``filter`` is a **trusted internal OData expression**. Day 15's tenant
    isolation must not interpolate a user-supplied value into this string; it
    has to be built from a typed authorization context and escaped there.
    """
    validate_search_arguments(query_text, query_vector, mode=mode, top=top, vector_k=vector_k)

    body: dict[str, Any] = {"top": top, "select": ",".join(SELECT_FIELDS)}
    if mode in TEXT_QUERY_MODES:
        body["search"] = query_text
    if mode in VECTOR_MODES:
        assert query_vector is not None  # narrowed by the validator
        body["vectorQueries"] = [
            {
                "kind": "vector",
                "vector": list(query_vector),
                "fields": VECTOR_FIELD,
                "k": vector_k,
            }
        ]
    if mode is SearchMode.HYBRID_SEMANTIC:
        body["queryType"] = "semantic"
        body["semanticConfiguration"] = SEMANTIC_CONFIGURATION_NAME
    if filter is not None:
        body["filter"] = filter
    return body


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
        # Read with .get() and compared via `is None`, not truthiness:
        # 0.0 is a real reranker verdict, not an absent one.
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
        # Ownership is decided once, here, and remembered: `aclose()` closes
        # only a connection pool this object created. Closing an injected one
        # would reach outside this object's lifetime and break whatever else
        # is sharing it — a test's MockTransport client, or an application-wide
        # pool a later composition point may hand in.
        self._owns_client = client is None
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

    async def aclose(self) -> None:
        """Release the connection pool, if this object is the one that made it.

        Idempotent, and a no-op for an injected client. Without this, a
        long-running tool has no way to shut a pool down except to wait for
        garbage collection, which is not a point in time anyone controls.
        """
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "AzureSearchClient":
        return self

    async def __aexit__(self, *exception: object) -> None:
        await self.aclose()

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


class FakeSearchClient:
    """An in-memory stand-in that scores lexically in **every** mode.

    What it proves: composition wiring, the arguments a caller passes, result
    shape, ordering mechanics, and the empty-result path. It applies exactly
    the same argument validation as the real adapter, so a call that passes
    here cannot fail against the service for a contract reason.

    What it deliberately does not do: simulate cosine similarity, RRF, or the
    semantic ranker. Day 12's fake embeddings carry no semantics, so scoring
    cosine over them would yield numbers that look like ranking evidence and
    are noise. Using lexical scoring for ``VECTOR`` mode is a knowingly
    unfaithful stand-in — an honest fake beats a plausible one. Retrieval
    quality observed here means nothing.

    It also does not apply ``filter``: the argument is recorded on
    ``last_filter`` so a caller can assert what was passed, but every document
    is still scored and can still be returned regardless of its value. A test
    written against this fake to assert "tenant B's chunks are not returned"
    would pass whether or not real filtering was ever wired up.
    """

    def __init__(self, documents: Sequence[dict[str, Any]] = ()) -> None:
        self._documents = list(documents)
        self.last_mode: SearchMode | None = None
        self.last_top: int | None = None
        self.last_vector_k: int | None = None
        self.last_filter: str | None = None

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
        validate_search_arguments(
            query_text, query_vector, mode=mode, top=top, vector_k=vector_k
        )
        self.last_mode = mode
        self.last_top = top
        self.last_vector_k = vector_k
        self.last_filter = filter

        terms = {term for term in query_text.lower().split() if term}
        scored: list[tuple[float, SearchHit]] = []
        for document in self._documents:
            haystack = " ".join(
                str(document.get(field, "")) for field in ("title", "heading_path", "content")
            ).lower()
            overlap = sum(1 for term in terms if term in haystack)
            if not overlap:
                continue
            scored.append(
                (
                    float(overlap),
                    SearchHit(
                        chunk_id=str(document.get("chunk_id", "")),
                        parent_id=str(document.get("parent_id", "")),
                        title=str(document.get("title", "")),
                        heading_path=str(document.get("heading_path", "")),
                        content=str(document.get("content", "")),
                        score=float(overlap),
                        reranker_score=None,  # never fabricated: see the docstring
                    ),
                )
            )
        # Ties break by chunk_id so ordering is reproducible across runs.
        scored.sort(key=lambda pair: (-pair[0], pair[1].chunk_id))
        return SearchResult(
            hits=tuple(hit for _, hit in scored[:top]),
            mode=mode,
            vector_k=vector_k if mode is not SearchMode.KEYWORD else None,
        )

    async def aclose(self) -> None:
        """Nothing owned; the fake never opens a client."""


def build_search_client(settings: Settings) -> SearchClient:
    """The one place fake and real are chosen. Handlers never branch on this."""
    if settings.use_fake_search:
        return FakeSearchClient()
    return AzureSearchClient(settings)


__all__ = [
    "SELECT_FIELDS",
    "AzureSearchClient",
    "FakeSearchClient",
    "SearchClient",
    "SearchConfigurationError",
    "SearchDiagnostics",
    "SearchError",
    "SearchRequestRejectedError",
    "SearchUnavailableError",
    "build_search_body",
    "build_search_client",
    "map_search_status",
    "parse_hits",
    "search_url",
]
