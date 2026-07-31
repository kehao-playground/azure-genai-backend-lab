"""Write-path orchestration for Azure AI Search: retry, enumeration,
replacement. No transport lives here — see `services/search_data_plane.py`.

This module deals in typed documents, actions and per-key outcomes. It never
spells a request: how documents are grouped into requests, how they are
serialized, and what a service calls each action all belong to the adapter,
which hands back batches this module treats as opaque apart from the keys they
carry. That is what lets the contracts below be tested without a network, and
what makes swapping REST for an SDK a change to one file.

Three contracts hold this together, and none substitutes for another:

* a per-key **terminal state** for every document in a batch (upsert *and*
  remove), so a retry's later success is representable without a duplicate;
* the **fail-closed gate** in `indexing_results.may_delete_stale()`;
* a per-`parent_id` **critical section**, because two replacements of one
  document can otherwise each succeed and still destroy each other's work.
"""

import asyncio
import logging
from collections import Counter
from collections.abc import Callable, Coroutine, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from azgenai_lab.core.errors import UpstreamError
from azgenai_lab.core.keyed_lock import KeyedLock
from azgenai_lab.models.rag import IndexingAction
from azgenai_lab.services.azure_search import SearchUnavailableError
from azgenai_lab.services.indexing_results import (
    Disposition,
    IndexingResult,
    classify,
    is_storage_alert,
    may_delete_stale,
)

logger = logging.getLogger(__name__)

# Bounded so a permanently sick service cannot spin forever. A module constant
# rather than a setting: nothing outside the indexing job reads it and no
# environment has a reason to differ.
MAX_INDEXING_ATTEMPTS = 3
_BACKOFF_SECONDS = 1.0


class DocumentBatch(Protocol):
    """As much of a request as this module is allowed to know.

    Just the keys travelling together. They are needed here because a
    request-level failure has no per-document verdict, so exactly this batch's
    keys — and no others — stay outstanding. Everything else about a batch is
    the adapter's: this module receives one from the planner and hands it back
    unread.
    """

    @property
    def keys(self) -> tuple[str, ...]: ...


# Documents grouped into sendable requests, and one such request sent. The two
# are a pair from the same adapter — the type parameter says so — because a
# batch is only meaningful to whatever built it.
type PlanBatches[BatchT] = Callable[
    [Sequence[dict[str, Any]], IndexingAction], Iterator[BatchT]
]
type PostBatch[BatchT] = Callable[[BatchT], Coroutine[Any, Any, list[IndexingResult]]]
type Sleeper = Callable[[float], Coroutine[Any, Any, None]]


class UnsendableDocumentError(UpstreamError):
    """A document cannot be made into a request, and no retry will change that.

    Raised by the planner before anything is sent. *Why* a document is
    unsendable is the adapter's to name — a size ceiling, a shape the service
    will not take — but the disposition belongs here: permanent, never
    retried, and never softened into an "outcome unknown" result, because
    nothing was ever sent.
    """

    status_code = 500
    code = "unsendable_document"
    message = "A document could not be prepared for indexing."


class DuplicateChunkIdError(UpstreamError):
    """Two documents in one indexing action share a chunk_id.

    Raised before anything is sent. Collapsing them keeps only the last
    document under each key, so the earlier one is never sent and its outcome
    never reported.
    """

    status_code = 500
    code = "duplicate_chunk_id"
    message = "An indexing action contained two documents with the same key."


async def run_indexing_with_retry[BatchT: DocumentBatch](
    plan_batches: PlanBatches[BatchT],
    post: PostBatch[BatchT],
    documents: Sequence[dict[str, Any]],
    action: IndexingAction,
    *,
    sleep: Sleeper = asyncio.sleep,
) -> dict[str, IndexingResult]:
    """Drive one indexing action to a single terminal result per key.

    The rule that makes retrying meaningful: *within one final collection* a
    duplicate key is fail-closed, but *across attempts* a key may legitimately
    move from a retryable failure to success. An upsert that succeeds has
    proved the key durable; refusing to record that would make the
    stale-delete gate unopenable after any retry.

    Only ``SearchUnavailableError`` is retried. A rejected request, a document
    the planner refuses, and a duplicate key are permanent and propagate:
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
    # Keyed per key, not one shared slot: a synthesized result below must
    # carry the error from *that key's own batch*, not whichever batch's
    # request-level failure the loop happened to process last — a key from a
    # batch that failed with one error must never be reported with a
    # different batch's message.
    last_error_by_key: dict[str, str] = {}

    for attempt in range(1, MAX_INDEXING_ATTEMPTS + 1):
        if not outstanding:
            break
        if attempt > 1:
            await sleep(_BACKOFF_SECONDS * (2 ** (attempt - 2)))

        still_outstanding: list[str] = []
        # Not inside the try: a document the planner refuses is a local,
        # permanent failure and must not be mistaken for a transient request
        # failure.
        for batch in plan_batches([by_key[key] for key in outstanding], action):
            try:
                results = await post(batch)
            except SearchUnavailableError as exc:
                # No per-document verdict exists for *this batch*, so exactly
                # this batch's keys stay outstanding. Requeuing the whole
                # attempt would re-send keys an earlier batch already settled.
                batch_error = str(exc.upstream_detail or exc.message)
                logger.warning(
                    "indexing request failed attempt=%d/%d action=%s keys=%s detail=%s",
                    attempt,
                    MAX_INDEXING_ATTEMPTS,
                    action,
                    ",".join(batch.keys),
                    batch_error,
                )
                still_outstanding.extend(batch.keys)
                for key in batch.keys:
                    last_error_by_key[key] = batch_error
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
                error_message=last_error_by_key.get(key, "indexing retries exhausted"),
            )
    return terminal


# Pages of chunk ids under one tenant's document, asked for in this project's
# terms: the tenant, the document, and the key to resume after. How that
# becomes a query — the OData expression, the ordering, the page size — is the
# adapter's business, and naming any of it here would put the write path's own
# request JSON in a module that is otherwise transport-free.
ListChunkIdPage = Callable[[str, str, str | None], Coroutine[Any, Any, list[str]]]


class EnumerationError(UpstreamError):
    """Enumeration could not be trusted, so it stopped instead of guessing."""

    status_code = 500
    code = "enumeration_failed"
    message = "Listing the indexed chunks of a document failed."


async def list_indexed_chunk_ids(
    list_page: ListChunkIdPage, tenant_id: str, parent_id: str
) -> list[str]:
    """List every ``chunk_id`` currently indexed under ``tenant_id``/``parent_id``.

    Pages by resuming strictly *after* the last key seen rather than by
    offset: an offset shifts when the index changes underneath the walk, so a
    concurrent write can make it repeat or miss rows. Resuming after a key
    removes that displacement — it does **not** provide snapshot isolation, and
    it is not what makes concurrent replacement safe. That is the critical
    section's job.

    Two conditions abort rather than continue, because both mean the walk has
    lost its footing: a key already seen, and a cursor that failed to advance.
    Continuing past either risks an unbounded loop, and this list decides what
    gets deleted.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    cursor: str | None = None

    while True:
        page = await list_page(tenant_id, parent_id, cursor)
        if not page:
            return ordered

        for chunk_id in page:
            if chunk_id in seen:
                raise EnumerationError(
                    f"chunk_id {chunk_id!r} was repeated while paging {parent_id!r}"
                )
            seen.add(chunk_id)
            ordered.append(chunk_id)

        # `max()` is Python's ordinal string comparison; the service applies
        # its own collation when it resumes after this key. If the two ever
        # disagreed, a row could be skipped without tripping either fail-closed
        # guard in this loop. Low risk today: chunk_id suffixes are a
        # fixed-width numeric string, so ordinal and typical collations agree
        # on their order.
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
    as success would hide it. A ``completed=True`` result rests on enumerating
    the index immediately after upload, and Azure AI Search indexing is
    eventually consistent — a stale chunk from a very recent prior failed run
    might not yet be queryable at that instant. This fails to the safe side
    (nothing is deleted that enumeration did not see), not the unsafe one, but
    ``completed`` is an observation made at one instant, not a guarantee that
    every stale chunk was caught.

    ``stale_state_unknown`` marks a different, worse case than either of the
    above: enumeration or deletion crashed outright *after* the upload had
    already landed, rather than running to completion and reporting a result.
    The new chunks are live either way, but here nothing observed whether any
    stale chunk was removed — unlike ``unresolved_stale_ids``, whose entries
    are stale chunks a normal, uncrashed deletion attempt is known to have
    left behind.
    """

    uploaded: tuple[IndexingResult, ...]
    deleted_keys: tuple[str, ...]
    unresolved_stale_ids: tuple[str, ...]
    completed: bool
    stale_state_unknown: bool = False


class DocumentReplacer[BatchT: DocumentBatch]:
    """Replace one document's chunks: upsert, gate, enumerate, remove.

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

    Both per-parent registries — the locks and the concurrency counters — are
    reclaimed as each replacement finishes. A corpus is enumerable by whoever
    can trigger indexing, so an entry per parent_id that is never removed
    grows with the corpus and never comes back.
    """

    def __init__(
        self,
        plan_batches: PlanBatches[BatchT],
        post_batch: PostBatch[BatchT],
        list_page: ListChunkIdPage,
        *,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        # Planner and poster arrive as a pair and are never mixed across
        # adapters: a batch means nothing to a transport that did not build it.
        self._plan_batches = plan_batches
        self._post_batch = post_batch
        self._list_page = list_page
        self._sleep = sleep
        self._locks = KeyedLock()
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

    @property
    def tracked_parent_count(self) -> int:
        """Parent ids still holding bookkeeping of any kind.

        Both registries are counted together because they must empty
        together. A lock entry reclaimed while a zero-valued concurrency
        counter survives (or the reverse) is still a leak whose size is set by
        how many documents have ever been replaced, just a leak that takes
        longer to notice.
        """
        return len(self._locks) + len(self._active)

    async def replace(
        self, tenant_id: str, parent_id: str, documents: Sequence[dict[str, Any]]
    ) -> ReplacementOutcome:
        async with self._locks.hold(parent_id):
            active = self._active.get(parent_id, 0) + 1
            self._active[parent_id] = active
            self.max_concurrent_per_parent = max(self.max_concurrent_per_parent, active)
            self._active_total += 1
            self.max_concurrent_replacements = max(
                self.max_concurrent_replacements, self._active_total
            )
            try:
                return await self._replace(tenant_id, parent_id, documents)
            finally:
                remaining = self._active[parent_id] - 1
                if remaining:
                    self._active[parent_id] = remaining
                else:
                    # Removed rather than left at zero. A counter kept for
                    # every parent_id ever replaced grows with the corpus and
                    # never shrinks, which is the same leak as an unreclaimed
                    # lock wearing different clothes.
                    del self._active[parent_id]
                self._active_total -= 1

    async def _replace(
        self, tenant_id: str, parent_id: str, documents: Sequence[dict[str, Any]]
    ) -> ReplacementOutcome:
        # Checked before anything is sent. Uploading documents belonging to one
        # tenant or parent while enumerating another turns a caller wiring
        # mistake into deletion: the upload succeeds, the gate passes on the
        # uploaded keys, and then every chunk of the *named* tenant/parent is
        # judged stale and removed. A mismatch on either key is never a
        # recoverable situation, so nothing goes out until both are ruled out
        # — a tenant_id mismatch is an isolation breach, not a type nit.
        for document in documents:
            owner_tenant = document.get("tenant_id")
            if owner_tenant != tenant_id:
                raise ValueError(
                    f"document {document.get('chunk_id')!r} has tenant_id {owner_tenant!r}, "
                    f"but replace() was called for {tenant_id!r}"
                )
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
            self._plan_batches,
            self._post_batch,
            documents,
            IndexingAction.UPSERT,
            sleep=self._sleep,
        )
        upload_results = tuple(uploaded[key] for key in expected_keys)

        # `upload_results` is built by indexing `uploaded` with
        # `expected_keys` just above, so the only condition `may_delete_stale`
        # can actually fail on *at this call site* is "not every key
        # succeeded" — that narrowing itself depends on the `if not
        # documents` guard above, which already ruled out the empty-
        # `expected_keys` case this gate would otherwise also have to cover.
        # Its other two guards — no duplicate/unexpected key and every key
        # answered exactly once — are enforced upstream instead: duplicates
        # are rejected before anything is sent (`DuplicateChunkIdError` in
        # `run_indexing_with_retry`), and that same function raises
        # `SearchUnavailableError` on a repeated or unexpected key in a
        # response, and returns a terminal result for every key it was
        # given — so a key missing from the response is a failure, not an
        # absence. Don't simplify those checks there on the assumption this
        # gate still covers them.
        if not may_delete_stale(upload_results, expected_keys=expected_keys):
            logger.warning(
                "stale deletion blocked parent_id=%s — old and new chunks both remain, "
                "which is recoverable; a re-run is required",
                parent_id,
            )
            return ReplacementOutcome(upload_results, (), (), False)

        # From here on the upload has already landed and is live. A failure
        # in the two calls below is not the recoverable "gate blocked, nothing
        # sent" case above, nor the "every key answered, some deletions
        # failed" case below — it means enumeration or deletion crashed
        # outright, so nothing observed whether any stale chunk survived.
        # Letting that escape as a raised exception would read like nothing
        # happened, when in fact new chunks are in and old ones might still
        # be there too. `UpstreamError` is caught broadly rather than just
        # `EnumerationError`/`SearchUnavailableError`, because a rejected or
        # misconfigured delete request (`SearchRequestRejectedError`,
        # `SearchConfigurationError`) leaves exactly the same ambiguity.
        try:
            indexed = await list_indexed_chunk_ids(self._list_page, tenant_id, parent_id)
            expected = set(expected_keys)
            stale = [key for key in indexed if key not in expected]
            if not stale:
                return ReplacementOutcome(upload_results, (), (), True)

            deletions = await run_indexing_with_retry(
                self._plan_batches,
                self._post_batch,
                [{"chunk_id": key} for key in stale],
                IndexingAction.REMOVE,
                sleep=self._sleep,
            )
        except (DuplicateChunkIdError, UnsendableDocumentError):
            # Permanent input faults, not cleanup failures — they must stay
            # unretried and propagate as raised exceptions, never soften into
            # a `stale_state_unknown` outcome the way a transport or config
            # failure below does.
            raise
        except UpstreamError as exc:
            logger.error(
                "stale cleanup crashed parent_id=%s after a successful upload — new "
                "chunks are live, stale state is unknown: %s",
                parent_id,
                exc.upstream_detail or exc.message,
            )
            return ReplacementOutcome(upload_results, (), (), False, True)

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
