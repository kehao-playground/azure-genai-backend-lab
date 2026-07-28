"""The write-side Azure AI Search boundary.

Everything REST about indexing lives here: URLs, the api-key header, the raw
request buffer, per-document 207 parsing, and the mapping from HTTP failures
to this project's search error vocabulary. `services/search_indexing.py` is
transport-free above it, which is what lets the orchestration contracts be
tested without a network and the transport be tested without orchestration.
"""

import logging
from typing import Any

import httpx

from azgenai_lab.core.config import Settings
from azgenai_lab.core.errors import ConfigurationError
from azgenai_lab.models.search_index import INDEX_NAME, SEARCH_API_VERSION, to_index_definition
from azgenai_lab.services.azure_search import SearchUnavailableError, map_search_status
from azgenai_lab.services.indexing_results import IndexingResult

logger = logging.getLogger(__name__)


def index_url(endpoint: str) -> str:
    return f"{endpoint.rstrip('/')}/indexes/{INDEX_NAME}?api-version={SEARCH_API_VERSION}"


def documents_url(endpoint: str) -> str:
    return (
        f"{endpoint.rstrip('/')}/indexes/{INDEX_NAME}/docs/index"
        f"?api-version={SEARCH_API_VERSION}"
    )


def search_documents_url(endpoint: str) -> str:
    return (
        f"{endpoint.rstrip('/')}/indexes/{INDEX_NAME}/docs/search"
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
                "azure_search_endpoint and azure_search_admin_key are required "
                "when use_fake_search is false"
            )
        endpoint = settings.azure_search_endpoint
        self._index_url = index_url(endpoint)
        self._documents_url = documents_url(endpoint)
        self._search_url = search_documents_url(endpoint)
        self._headers = {
            "api-key": settings.azure_search_admin_key.get_secret_value(),
            "content-type": "application/json",
        }
        self._client = client or httpx.AsyncClient(timeout=settings.llm_timeout_seconds)

    async def create_or_update_index(self) -> None:
        response = await self._send("PUT", self._index_url, json=to_index_definition())
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

    async def post_index(self, body: bytes) -> list[IndexingResult]:
        """Send one pre-serialized batch and read its per-document verdicts.

        ``body`` is passed through as raw content, never re-encoded: the
        batcher measured these exact bytes against the 16 MB ceiling.
        """
        response = await self._send("POST", self._documents_url, content=body)
        if response.status_code >= 400:
            raise self._error(response)
        return parse_indexing_results(self._json(response))

    async def post_search(self, body: dict[str, Any]) -> list[str]:
        """Run a query and return just the chunk ids, in response order."""
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
                raise SearchUnavailableError(f"enumeration result has no chunk_id: {entry!r}")
            keys.append(chunk_id)
        return keys

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._client.request(method, url, headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise SearchUnavailableError(str(exc)) from exc

    def _error(self, response: httpx.Response) -> Exception:
        return map_search_status(
            response.status_code, response.text[:500], response.headers.get("request-id")
        )

    @staticmethod
    def _json(response: httpx.Response) -> object:
        try:
            return response.json()
        except ValueError as exc:
            raise SearchUnavailableError(f"response body was not JSON: {exc}") from exc
