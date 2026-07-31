"""The write-side Azure AI Search boundary.

Everything REST about indexing lives here: URLs, the api-key header, the
indexing request body and the `@search.action` vocabulary inside it, the
request-size ceilings that decide how many documents fit in one call, the
OData expression that enumerates one document's chunks, per-document 207
parsing, and the mapping from HTTP failures to this project's search error
vocabulary. `services/search_indexing.py` is transport-free above it — it
hands down typed documents and an `IndexingAction`, asks for "the chunks of
this document, resuming after this key", and never spells a filter, a field
name or a batch — which is what lets the orchestration contracts be tested
without a network and the transport be tested without orchestration.

The import of `UnsendableDocumentError` runs in that direction too: the
orchestration owns what a permanent, nothing-was-sent failure *means*, and
this module supplies the one reason it can happen here.

A 404 from `post_batch` (or from `create_or_update_index`/`delete_index`)
maps, via `map_search_status`, to `SearchConfigurationError` — a missing
index is a configuration failure, not a transient one. That class
deliberately does not subclass `SearchError`, so a retry coordinator written
as `except SearchError:` will not retry it; only `UpstreamError` catches
both branches.
"""

import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from azgenai_lab.core.config import Settings
from azgenai_lab.core.errors import ConfigurationError, UpstreamError
from azgenai_lab.models.rag import IndexingAction
from azgenai_lab.models.search_index import INDEX_NAME, SEARCH_API_VERSION, to_index_definition
from azgenai_lab.services.azure_search import SearchUnavailableError, map_search_status, search_url
from azgenai_lab.services.indexing_results import IndexingResult
from azgenai_lab.services.odata import escape_odata_literal
from azgenai_lab.services.search_indexing import UnsendableDocumentError

logger = logging.getLogger(__name__)

# The service returns 50 results by default and at most 1,000 per page.
ENUMERATION_PAGE_SIZE = 1_000

# Documented API request limits (checked 2026-07): at most 1,000 documents per
# batch of uploads, merges or deletes, and a 16 MB payload ceiling applying to
# the whole request.
MAX_BATCH_DOCUMENTS = 1_000
MAX_REQUEST_BYTES = 16 * 1024 * 1024

_OPEN = b'{"value":['
_CLOSE = b"]}"
_WRAPPER_BYTES = len(_OPEN) + len(_CLOSE)

# The `@search.action` spelling of each action this project performs. This
# table is the reason the vocabulary stops here: "upload" is the service's
# word for an upsert, and nothing above this module says it.
_WIRE_ACTIONS = {
    IndexingAction.UPSERT: "upload",
    IndexingAction.REMOVE: "delete",
}


class DocumentTooLargeError(UnsendableDocumentError):
    """One document cannot fit in a request on its own.

    Raised before anything is sent, and **not** retryable: no number of
    attempts makes an oversized document smaller. The retry coordinator
    deliberately does not catch this.
    """

    code = "document_too_large"
    message = "A document exceeds the maximum indexing request size."


@dataclass(frozen=True)
class IndexingBatch:
    """One request body, exactly the keys it carries, and what it does to them.

    The keys travel with the body because a request-level failure must requeue
    only this batch. Requeuing the whole attempt would re-send keys an earlier
    batch already settled as succeeded.

    ``body`` is this module's business. The orchestration above holds a batch
    only to hand it back to :meth:`SearchDataPlane.post_batch`, and reads
    nothing but ``keys`` — which is what keeps the buffer that was measured
    and the buffer that travels the same object.
    """

    body: bytes
    keys: tuple[str, ...]
    action: IndexingAction


def plan_batches(
    documents: Sequence[dict[str, Any]], action: IndexingAction
) -> Iterator[IndexingBatch]:
    """Group documents into request bodies, each within both ceilings.

    Serialize **once**. The yielded ``body`` is exactly what gets sent —
    :meth:`SearchDataPlane.post_batch` passes it as raw content rather than
    handing Python objects back to an encoder, because two serializations of
    the same object are not guaranteed to be byte-identical, and then the
    limit guarded here would not be the limit that travels.
    """
    wire_action = _WIRE_ACTIONS[action]
    encoded: list[tuple[str, bytes]] = []
    for document in documents:
        key = str(document["chunk_id"])
        payload = {**document, "@search.action": wire_action}
        blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if _WRAPPER_BYTES + len(blob) > MAX_REQUEST_BYTES:
            raise DocumentTooLargeError(
                f"document {key} serializes to {len(blob)} bytes, above the "
                f"{MAX_REQUEST_BYTES} byte request limit"
            )
        encoded.append((key, blob))

    batch: list[bytes] = []
    keys: list[str] = []
    size = _WRAPPER_BYTES
    for key, blob in encoded:
        # The separating comma is payload too, and has to be counted or a
        # batch can land one byte over the ceiling.
        extra = len(blob) + (1 if batch else 0)
        if batch and (len(batch) >= MAX_BATCH_DOCUMENTS or size + extra > MAX_REQUEST_BYTES):
            yield IndexingBatch(_OPEN + b",".join(batch) + _CLOSE, tuple(keys), action)
            batch, keys, size = [], [], _WRAPPER_BYTES
            extra = len(blob)
        batch.append(blob)
        keys.append(key)
        size += extra
    if batch:
        yield IndexingBatch(_OPEN + b",".join(batch) + _CLOSE, tuple(keys), action)


def index_url(endpoint: str) -> str:
    return f"{endpoint.rstrip('/')}/indexes/{INDEX_NAME}?api-version={SEARCH_API_VERSION}"


def documents_url(endpoint: str) -> str:
    return (
        f"{endpoint.rstrip('/')}/indexes/{INDEX_NAME}/docs/index"
        f"?api-version={SEARCH_API_VERSION}"
    )


def parse_indexing_results(payload: object) -> list[IndexingResult]:
    """Read a 200 or 207 body document by document.

    Every field is required. A result whose ``status`` is missing cannot be
    classified, and guessing would either strand a document or open the
    stale-delete gate on a fiction.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
        raise SearchUnavailableError("indexing response has no 'value' array")
    results: list[IndexingResult] = []
    for entry in payload["value"]:
        if not isinstance(entry, dict):
            raise SearchUnavailableError("indexing result was not an object")
        key, status, status_code = entry.get("key"), entry.get("status"), entry.get("statusCode")
        if not isinstance(key, str) or not isinstance(status, bool):
            raise SearchUnavailableError(f"indexing result has no usable key/status: {entry!r}")
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            raise SearchUnavailableError(f"indexing result has no statusCode: {entry!r}")
        message = entry.get("errorMessage")
        results.append(
            IndexingResult(
                key=key,
                status=status,
                status_code=status_code,
                error_message=message if isinstance(message, str) else None,
            )
        )
    return results


class SearchDataPlane:
    """Index management plus the indexing and enumeration calls."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        if not settings.azure_search_endpoint or not settings.azure_search_admin_key:
            raise ConfigurationError(
                "azure_search_endpoint and azure_search_admin_key are required"
            )
        endpoint = settings.azure_search_endpoint
        self._index_url = index_url(endpoint)
        self._documents_url = documents_url(endpoint)
        # Same URL shape the query-side client builds — `search_url()` is the
        # one owner of it, imported rather than duplicated here.
        self._search_url = search_url(endpoint)
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

    async def aclose(self) -> None:
        """Release the connection pool, if this object is the one that made it.

        Idempotent, and a no-op for an injected client. Without this, a
        long-running tool has no way to shut a pool down except to wait for
        garbage collection, which is not a point in time anyone controls.
        """
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "SearchDataPlane":
        return self

    async def __aexit__(self, *exception: object) -> None:
        await self.aclose()

    async def create_or_update_index(self) -> None:
        # The REST contract requires `Prefer: return=representation` on this
        # PUT: it is what tells the service to answer with the created or
        # updated resource (and with 201 on create, 200 on update) rather
        # than a bare 204. No other call here is a PUT, so this header is
        # kept off the shared header dict and sent only for this request.
        response = await self._send(
            "PUT",
            self._index_url,
            json=to_index_definition(),
            extra_headers={"Prefer": "return=representation"},
        )
        if response.status_code >= 400:
            raise self._error(response)
        logger.info("index created or updated name=%s", INDEX_NAME)

    async def delete_index(self) -> None:
        """Idempotent: an index that is already gone is a successful teardown."""
        response = await self._send("DELETE", self._index_url)
        if response.status_code == 404:
            logger.info("index already absent name=%s", INDEX_NAME)
            return
        if response.status_code >= 400:
            raise self._error(response)
        logger.info("index deleted name=%s", INDEX_NAME)

    async def post_batch(self, batch: IndexingBatch) -> list[IndexingResult]:
        """Send one planned batch and read its per-document verdicts.

        ``batch.body`` is passed through as raw content, never re-encoded:
        `plan_batches()` measured these exact bytes against the 16 MB ceiling.
        """
        response = await self._send("POST", self._documents_url, content=batch.body)
        if response.status_code >= 400:
            raise self._error(response)
        return parse_indexing_results(self._json(response))

    async def list_chunk_ids(self, parent_id: str, after: str | None = None) -> list[str]:
        """One page of chunk ids under ``parent_id``, resuming after ``after``.

        The range filter is strictly ``gt``. The official paging example uses
        ``ge``, which repeats the previous page's last key; a caller that then
        re-derives its cursor from that repeat never advances at all — a silent
        infinite loop rather than one duplicate row.

        ``search=*`` with a ``select`` of the key alone: nothing here needs the
        content, and the caller is computing a set difference, not ranking.
        """
        expression = f"parent_id eq '{escape_odata_literal(parent_id)}'"
        if after is not None:
            expression = f"{expression} and chunk_id gt '{escape_odata_literal(after)}'"
        body: dict[str, Any] = {
            "search": "*",
            "filter": expression,
            "select": "chunk_id",
            "orderby": "chunk_id asc",
            "top": ENUMERATION_PAGE_SIZE,
        }
        response = await self._send("POST", self._search_url, json=body)
        if response.status_code >= 400:
            raise self._error(response)
        payload = self._json(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
            raise SearchUnavailableError("enumeration response has no 'value' array")
        keys: list[str] = []
        for entry in payload["value"]:
            chunk_id = entry.get("chunk_id") if isinstance(entry, dict) else None
            if not isinstance(chunk_id, str):
                shape = sorted(entry) if isinstance(entry, dict) else type(entry).__name__
                raise SearchUnavailableError(f"enumeration result has no chunk_id: {shape!r}")
            keys.append(chunk_id)
        return keys

    async def _send(
        self,
        method: str,
        url: str,
        *,
        extra_headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        # Lowercased on both sides: HTTP headers are case-insensitive but a
        # plain dict merge is not, so an `extra_headers` key differing only in
        # case from a shared one would duplicate on the wire instead of
        # replacing it.
        headers = (
            {
                **{key.lower(): value for key, value in self._headers.items()},
                **{key.lower(): value for key, value in extra_headers.items()},
            }
            if extra_headers
            else self._headers
        )
        try:
            return await self._client.request(method, url, headers=headers, **kwargs)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise SearchUnavailableError(str(exc)) from exc

    def _error(self, response: httpx.Response) -> UpstreamError:
        return map_search_status(
            response.status_code, response.text[:500], response.headers.get("request-id")
        )

    @staticmethod
    def _json(response: httpx.Response) -> object:
        try:
            return response.json()
        except ValueError as exc:
            # Kept even on this failure path, the same as the query-side
            # client: it is the one field that correlates a live failure with
            # the service, and dropping it here just because the body did not
            # parse would lose it for no reason tied to the failure itself.
            raise SearchUnavailableError(
                f"response body was not JSON: {exc}",
                request_id=response.headers.get("request-id"),
            ) from exc


__all__ = [
    "ENUMERATION_PAGE_SIZE",
    "MAX_BATCH_DOCUMENTS",
    "MAX_REQUEST_BYTES",
    "DocumentTooLargeError",
    "IndexingBatch",
    "SearchDataPlane",
    "documents_url",
    "index_url",
    "parse_indexing_results",
    "plan_batches",
]
