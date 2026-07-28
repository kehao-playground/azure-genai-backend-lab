"""Write-path orchestration for Azure AI Search: batching, retry, enumeration,
replacement. No transport lives here — see `services/search_data_plane.py`.

Three contracts hold this together, and none substitutes for another:

* a per-key **terminal state** for every document in a batch (upload *and*
  delete), so a retry's later success is representable without a duplicate;
* the **fail-closed gate** in `indexing_results.may_delete_stale()`;
* a per-`parent_id` **critical section**, because two replacements of one
  document can otherwise each succeed and still destroy each other's work.
"""

import asyncio
import json
import logging
from collections import Counter
from collections.abc import Callable, Coroutine, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from azgenai_lab.core.errors import UpstreamError
from azgenai_lab.services.azure_search import SearchUnavailableError
from azgenai_lab.services.indexing_results import (
    Disposition,
    IndexingResult,
    classify,
    is_storage_alert,
    may_delete_stale,
)

logger = logging.getLogger(__name__)

# Documented API request limits (checked 2026-07): at most 1,000 documents per
# batch of uploads, merges or deletes, and a 16 MB payload ceiling applying to
# the whole request.
MAX_BATCH_DOCUMENTS = 1_000
MAX_REQUEST_BYTES = 16 * 1024 * 1024

# Bounded so a permanently sick service cannot spin forever. A module constant
# rather than a setting: nothing outside the indexing job reads it and no
# environment has a reason to differ.
MAX_INDEXING_ATTEMPTS = 3
_BACKOFF_SECONDS = 1.0

_OPEN = b'{"value":['
_CLOSE = b"]}"
_WRAPPER_BYTES = len(_OPEN) + len(_CLOSE)


class DocumentTooLargeError(UpstreamError):
    """One document cannot fit in a request on its own.

    Raised before anything is sent, and **not** retryable: no number of
    attempts makes an oversized document smaller. The retry coordinator
    deliberately does not catch this.
    """

    status_code = 500
    code = "document_too_large"
    message = "A document exceeds the maximum indexing request size."


class DuplicateChunkIdError(UpstreamError):
    """Two documents in one indexing action share a chunk_id.

    Raised before anything is sent. Collapsing them keeps only the last
    document under each key, so the earlier one is never sent and its outcome
    never reported.
    """

    status_code = 500
    code = "duplicate_chunk_id"
    message = "An indexing action contained two documents with the same key."


@dataclass(frozen=True)
class IndexingBatch:
    """One request body and exactly the keys it carries.

    The keys travel with the body because a request-level failure must requeue
    only this batch. Requeuing the whole attempt would re-send keys an earlier
    batch already settled as succeeded.
    """

    body: bytes
    keys: tuple[str, ...]


def serialize_batches(
    documents: Sequence[dict[str, Any]], action: str
) -> Iterator[IndexingBatch]:
    """Yield request bodies, each within both ceilings.

    Serialize **once**. The yielded ``body`` is exactly what gets sent — the
    caller passes it as raw content rather than handing Python objects back to
    an encoder, because two serializations of the same object are not
    guaranteed to be byte-identical, and then the limit guarded here would not
    be the limit that travels.
    """
    encoded: list[tuple[str, bytes]] = []
    for document in documents:
        key = str(document["chunk_id"])
        payload = {**document, "@search.action": action}
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
            yield IndexingBatch(_OPEN + b",".join(batch) + _CLOSE, tuple(keys))
            batch, keys, size = [], [], _WRAPPER_BYTES
            extra = len(blob)
        batch.append(blob)
        keys.append(key)
        size += extra
    if batch:
        yield IndexingBatch(_OPEN + b",".join(batch) + _CLOSE, tuple(keys))


PostBatch = Callable[[bytes], Coroutine[Any, Any, list[IndexingResult]]]
Sleeper = Callable[[float], Coroutine[Any, Any, None]]


async def run_indexing_with_retry(
    post: PostBatch,
    documents: Sequence[dict[str, Any]],
    action: str,
    *,
    sleep: Sleeper = asyncio.sleep,
) -> dict[str, IndexingResult]:
    """Drive one indexing action to a single terminal result per key.

    The rule that makes retrying meaningful: *within one final collection* a
    duplicate key is fail-closed, but *across attempts* a key may legitimately
    move from a retryable failure to success. An upsert that succeeds has
    proved the key durable; refusing to record that would make the
    stale-delete gate unopenable after any retry.

    Only ``SearchUnavailableError`` is retried. A rejected request, an
    oversized document, and a duplicate key are permanent and propagate:
    repeating any of them just delays the report. Permanent per-document
    failures are terminal and are never re-sent or overwritten. Exhausted
    retries keep the last retryable failure, and a request-level failure with
    no per-document answer synthesizes one, so the gate stays shut either way.
    """
    keys_in_order = [str(document["chunk_id"]) for document in documents]
    duplicate_keys = {key for key, count in Counter(keys_in_order).items() if count > 1}
    if duplicate_keys:
        # Collapsing these into a dict would keep only the last document under
        # each key, so the earlier one is silently never sent and its outcome
        # never reported — the exact erasure `may_delete_stale()` refuses to
        # perform, one layer upstream of where it can catch it.
        raise DuplicateChunkIdError(
            f"duplicate chunk_id in indexing input: {sorted(duplicate_keys)}"
        )
    by_key = dict(zip(keys_in_order, documents, strict=True))
    terminal: dict[str, IndexingResult] = {}
    outstanding = list(by_key)
    last_error: str | None = None

    for attempt in range(1, MAX_INDEXING_ATTEMPTS + 1):
        if not outstanding:
            break
        if attempt > 1:
            await sleep(_BACKOFF_SECONDS * (2 ** (attempt - 2)))

        still_outstanding: list[str] = []
        # Not inside the try: an oversized document is a local, permanent
        # failure and must not be mistaken for a transient request failure.
        for batch in serialize_batches([by_key[key] for key in outstanding], action):
            try:
                results = await post(batch.body)
            except SearchUnavailableError as exc:
                # No per-document verdict exists for *this batch*, so exactly
                # this batch's keys stay outstanding. Requeuing the whole
                # attempt would re-send keys an earlier batch already settled.
                last_error = str(exc.upstream_detail or exc.message)
                logger.warning(
                    "indexing request failed attempt=%d/%d action=%s keys=%s detail=%s",
                    attempt,
                    MAX_INDEXING_ATTEMPTS,
                    action,
                    ",".join(batch.keys),
                    last_error,
                )
                still_outstanding.extend(batch.keys)
                continue

            returned_keys = [result.key for result in results]
            if len(returned_keys) != len(set(returned_keys)):
                # Fail closed *before* any terminal state is touched. Two
                # verdicts for one key in one response cannot be resolved, and
                # letting the last one win is precisely how an unresolved
                # transient failure gets papered over by a later success —
                # the thing `may_delete_stale()` refuses to do downstream.
                raise SearchUnavailableError(
                    f"indexing response repeated a key: {sorted(returned_keys)}"
                )
            returned = set(returned_keys)
            unexpected = returned - set(batch.keys)
            if unexpected:
                raise SearchUnavailableError(
                    f"indexing response mentioned keys that were not sent: {sorted(unexpected)}"
                )
            for result in results:
                if is_storage_alert(result):
                    logger.error(
                        "indexing throttled key=%s action=%s — usually a storage-capacity "
                        "signal rather than a request-rate one",
                        result.key,
                        action,
                    )
                terminal[result.key] = result
                if classify(result) is Disposition.RETRYABLE:
                    still_outstanding.append(result.key)
            # A key that was sent but never mentioned in the response has no
            # verdict; keep it outstanding rather than assume either way.
            still_outstanding.extend(key for key in batch.keys if key not in returned)

        outstanding = still_outstanding

    for key in outstanding:
        existing = terminal.get(key)
        # A key still outstanding here can only be retryable or absent —
        # SUCCEEDED and PERMANENT keys are never left in `outstanding`.
        if existing is None:
            terminal[key] = IndexingResult(
                key=key,
                status=False,
                status_code=503,
                error_message=last_error or "indexing retries exhausted",
            )
    return terminal


# The service returns 50 results by default and at most 1,000 per page.
ENUMERATION_PAGE_SIZE = 1_000

PostSearch = Callable[[dict[str, Any]], Coroutine[Any, Any, list[str]]]


class EnumerationError(UpstreamError):
    """Enumeration could not be trusted, so it stopped instead of guessing."""

    status_code = 500
    code = "enumeration_failed"
    message = "Listing the indexed chunks of a document failed."


def escape_odata_literal(value: str) -> str:
    """Escape a value for an OData string literal (a single quote doubles)."""
    return value.replace("'", "''")


async def list_indexed_chunk_ids(post_search: PostSearch, parent_id: str) -> list[str]:
    """List every ``chunk_id`` currently indexed under ``parent_id``.

    Pages with ``orderby`` plus a strict ``gt`` range filter rather than
    ``skip``: ``skip`` shifts when the index changes underneath the walk, so a
    concurrent write can make it repeat or miss rows. The cursor removes that
    displacement — it does **not** provide snapshot isolation, and it is not
    what makes concurrent replacement safe. That is the critical section's job.

    Two conditions abort rather than continue, because both mean the walk has
    lost its footing: a key already seen, and a cursor that failed to advance.
    Continuing past either risks an unbounded loop, and this list decides what
    gets deleted.
    """
    literal = escape_odata_literal(parent_id)
    seen: set[str] = set()
    ordered: list[str] = []
    cursor: str | None = None

    while True:
        expression = f"parent_id eq '{literal}'"
        if cursor is not None:
            expression = f"{expression} and chunk_id gt '{escape_odata_literal(cursor)}'"
        page = await post_search(
            {
                "search": "*",
                "filter": expression,
                "select": "chunk_id",
                "orderby": "chunk_id asc",
                "top": ENUMERATION_PAGE_SIZE,
            }
        )
        if not page:
            return ordered

        for chunk_id in page:
            if chunk_id in seen:
                raise EnumerationError(
                    f"chunk_id {chunk_id!r} was repeated while paging {parent_id!r}"
                )
            seen.add(chunk_id)
            ordered.append(chunk_id)

        next_cursor = max(page)
        if cursor is not None and next_cursor <= cursor:
            raise EnumerationError(
                f"cursor did not advance past {cursor!r} while paging {parent_id!r}"
            )
        cursor = next_cursor


@dataclass(frozen=True)
class ReplacementOutcome:
    """What actually happened to one document's chunks.

    ``completed`` is the honest field. Uploading successfully and then failing
    to remove stale chunks leaves the index readable but wrong; reporting that
    as success would hide it.
    """

    uploaded: tuple[IndexingResult, ...]
    deleted_keys: tuple[str, ...]
    unresolved_stale_ids: tuple[str, ...]
    completed: bool


class DocumentReplacer:
    """Replace one document's chunks: upload, gate, enumerate, delete.

    Every step for a given ``parent_id`` runs inside one critical section. Two
    concurrent replacements of the same document can otherwise each pass their
    own gate and then delete each other's chunks — with every request
    returning 200 and every page correct, so no error handling is triggered.
    That is the failure mode this lock exists for, and it is invisible to
    monitoring precisely because nothing fails.

    The lock's scope is **this process**. A deployment running more than one
    worker needs a durable lease, a generation field on the document, or a
    compare-and-set on that generation; an in-process lock does not span
    processes and must not be mistaken for one that does.
    """

    def __init__(
        self,
        post_index: PostBatch,
        post_search: PostSearch,
        *,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self._post_index = post_index
        self._post_search = post_search
        self._sleep = sleep
        self._locks: dict[str, asyncio.Lock] = {}
        self._active: dict[str, int] = {}
        # Peak observed concurrency for any single parent_id. Exposed so the
        # mutual exclusion can be asserted directly instead of inferred from
        # surviving keys, which cannot distinguish a legal serialization from
        # a corrupted one.
        self.max_concurrent_per_parent = 0
        self._active_total = 0
        # Peak observed concurrency across *all* parent_ids combined, not
        # keyed by anything. A lock narrowed from per-parent to a single
        # global critical section still leaves `max_concurrent_per_parent`
        # at 1 — that counter is keyed by parent_id regardless of how the
        # lock itself is scoped — so only this unkeyed counter can catch
        # that regression.
        self.max_concurrent_replacements = 0

    def _lock_for(self, parent_id: str) -> asyncio.Lock:
        lock = self._locks.get(parent_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[parent_id] = lock
        return lock

    async def replace(
        self, parent_id: str, documents: Sequence[dict[str, Any]]
    ) -> ReplacementOutcome:
        async with self._lock_for(parent_id):
            active = self._active.get(parent_id, 0) + 1
            self._active[parent_id] = active
            self.max_concurrent_per_parent = max(self.max_concurrent_per_parent, active)
            self._active_total += 1
            self.max_concurrent_replacements = max(
                self.max_concurrent_replacements, self._active_total
            )
            try:
                return await self._replace(parent_id, documents)
            finally:
                self._active[parent_id] -= 1
                self._active_total -= 1

    async def _replace(
        self, parent_id: str, documents: Sequence[dict[str, Any]]
    ) -> ReplacementOutcome:
        # Checked before anything is sent. Uploading documents belonging to one
        # parent while enumerating another turns a caller wiring mistake into
        # deletion: the upload succeeds, the gate passes on the uploaded keys,
        # and then every chunk of the *named* parent is judged stale and
        # removed. A mismatch is never a recoverable situation, so nothing goes
        # out until it is ruled out.
        for document in documents:
            owner = document.get("parent_id")
            if owner != parent_id:
                raise ValueError(
                    f"document {document.get('chunk_id')!r} has parent_id {owner!r}, "
                    f"but replace() was called for {parent_id!r}"
                )
        if not documents:
            # `may_delete_stale()` already fails closed on an empty
            # `expected_keys`, but its log message below talks about a
            # re-run fixing things — there is nothing to re-run when the
            # caller sent zero chunks. An empty chunk list is indistinguishable
            # from a chunking bug, so replacement is not how a document gets
            # retired; refuse it here, before anything is sent, with a message
            # that says so plainly.
            raise ValueError(
                f"replace() called for {parent_id!r} with no chunks — replacing a document "
                "with zero chunks does not retire it, and a re-run will not help"
            )
        expected_keys = [str(document["chunk_id"]) for document in documents]
        uploaded = await run_indexing_with_retry(
            self._post_index, documents, "upload", sleep=self._sleep
        )
        upload_results = tuple(uploaded[key] for key in expected_keys)

        # `upload_results` is built by indexing `uploaded` with
        # `expected_keys` just above, so the only condition `may_delete_stale`
        # can actually fail on *at this call site* is "not every key
        # succeeded". Its other two guards — no duplicate/unexpected key and
        # every key answered exactly once — are enforced upstream instead:
        # duplicates are rejected before anything is sent
        # (`DuplicateChunkIdError` in `run_indexing_with_retry`), and that
        # same function raises `SearchUnavailableError` on a repeated or
        # unexpected key in a response, and synthesizes a terminal result for
        # every key it returns. Don't simplify those checks there on the
        # assumption this gate still covers them.
        if not may_delete_stale(upload_results, expected_keys=expected_keys):
            logger.warning(
                "stale deletion blocked parent_id=%s — old and new chunks both remain, "
                "which is recoverable; a re-run is required",
                parent_id,
            )
            return ReplacementOutcome(upload_results, (), (), False)

        indexed = await list_indexed_chunk_ids(self._post_search, parent_id)
        expected = set(expected_keys)
        stale = [key for key in indexed if key not in expected]
        if not stale:
            return ReplacementOutcome(upload_results, (), (), True)

        deletions = await run_indexing_with_retry(
            self._post_index,
            [{"chunk_id": key} for key in stale],
            "delete",
            sleep=self._sleep,
        )
        deleted = tuple(
            key for key in stale if classify(deletions[key]) is Disposition.SUCCEEDED
        )
        unresolved = tuple(key for key in stale if key not in set(deleted))
        if unresolved:
            logger.error(
                "stale chunks survived parent_id=%s unresolved=%s — the replacement is "
                "not complete",
                parent_id,
                ",".join(unresolved),
            )
        return ReplacementOutcome(upload_results, deleted, unresolved, not unresolved)
