"""Every key ends with exactly one terminal result, across however many
attempts it took.
"""

import pytest

from azgenai_lab.models.rag import IndexingAction
from azgenai_lab.services.azure_search import (
    SearchRequestRejectedError,
    SearchUnavailableError,
)
from azgenai_lab.services.indexing_results import Disposition, IndexingResult, classify
from azgenai_lab.services.search_data_plane import (
    MAX_BATCH_DOCUMENTS,
    MAX_REQUEST_BYTES,
    DocumentTooLargeError,
    IndexingBatch,
    plan_batches,
)
from azgenai_lab.services.search_indexing import (
    _BACKOFF_SECONDS,
    MAX_INDEXING_ATTEMPTS,
    DuplicateChunkIdError,
    run_indexing_with_retry,
)

DOCUMENTS = [{"chunk_id": "a"}, {"chunk_id": "b"}]


async def _no_sleep(_seconds: float) -> None:
    return None


def _ok(key: str) -> IndexingResult:
    return IndexingResult(key=key, status=True, status_code=200)


def _retryable(key: str) -> IndexingResult:
    return IndexingResult(key=key, status=False, status_code=503, error_message="busy")


def _permanent(key: str) -> IndexingResult:
    return IndexingResult(key=key, status=False, status_code=400, error_message="bad")


def _keys(batch: IndexingBatch) -> list[str]:
    # `plan_batches` is the real planner here, and its own tests pin that a
    # batch's keys are exactly the documents its body carries — so reading
    # them from the batch is reading what was sent, not a second opinion.
    return list(batch.keys)


async def test_a_transient_failure_that_later_succeeds_ends_as_succeeded() -> None:
    # A retryable failure followed by a success must settle as one terminal
    # SUCCEEDED result, or the stale-delete gate can never open after any
    # retry.
    attempts: list[int] = []

    async def post(_batch: IndexingBatch) -> list[IndexingResult]:
        attempts.append(1)
        return [_ok("a"), _retryable("b")] if len(attempts) == 1 else [_ok("b")]

    results = await run_indexing_with_retry(
        plan_batches, post, DOCUMENTS, IndexingAction.UPSERT, sleep=_no_sleep
    )
    assert set(results) == {"a", "b"}
    assert all(classify(r) is Disposition.SUCCEEDED for r in results.values())
    assert len(attempts) == 2


async def test_only_the_retryable_key_is_resent() -> None:
    sent: list[list[str]] = []

    async def post(batch: IndexingBatch) -> list[IndexingResult]:
        sent.append(_keys(batch))
        return [_ok("a"), _retryable("b")] if len(sent) == 1 else [_ok("b")]

    await run_indexing_with_retry(
        plan_batches, post, DOCUMENTS, IndexingAction.UPSERT, sleep=_no_sleep
    )
    assert sent == [["a", "b"], ["b"]]


async def test_a_permanent_failure_is_never_retried_or_overwritten() -> None:
    attempts: list[int] = []

    async def post(_batch: IndexingBatch) -> list[IndexingResult]:
        attempts.append(1)
        return [_ok("a"), _permanent("b")]

    results = await run_indexing_with_retry(
        plan_batches, post, DOCUMENTS, IndexingAction.UPSERT, sleep=_no_sleep
    )
    assert len(attempts) == 1
    assert classify(results["b"]) is Disposition.PERMANENT


async def test_exhausted_retries_keep_the_last_retryable_failure() -> None:
    # "b" fails retryably on every attempt it is sent in. Once "a" succeeds on
    # attempt 1, later batches carry only "b" — the response must answer only
    # for the keys it was actually sent, or it trips the "response mentioned
    # keys that were not sent" guard.
    verdicts = {"a": _ok("a"), "b": _retryable("b")}
    attempts: list[list[str]] = []

    async def post(batch: IndexingBatch) -> list[IndexingResult]:
        keys = _keys(batch)
        attempts.append(keys)
        return [verdicts[key] for key in keys]

    results = await run_indexing_with_retry(
        plan_batches, post, DOCUMENTS, IndexingAction.UPSERT, sleep=_no_sleep
    )
    assert classify(results["b"]) is Disposition.RETRYABLE
    assert results["b"].error_message == "busy"
    assert len(attempts) == MAX_INDEXING_ATTEMPTS


async def test_request_level_failure_resends_that_batch() -> None:
    sent: list[list[str]] = []

    async def post(batch: IndexingBatch) -> list[IndexingResult]:
        sent.append(_keys(batch))
        if len(sent) == 1:
            raise SearchUnavailableError("503 from the gateway")
        return [_ok("a"), _ok("b")]

    results = await run_indexing_with_retry(
        plan_batches, post, DOCUMENTS, IndexingAction.UPSERT, sleep=_no_sleep
    )
    assert all(classify(r) is Disposition.SUCCEEDED for r in results.values())
    assert sent == [["a", "b"], ["a", "b"]]


async def test_a_failed_batch_does_not_resend_an_earlier_succeeded_batch() -> None:
    # Two batches in one attempt: batch 1 succeeds, batch 2 fails at the
    # request level. Requeuing the whole attempt would re-send batch 1's keys,
    # violating "SUCCEEDED is never re-sent".
    documents = [{"chunk_id": f"k{i}"} for i in range(MAX_BATCH_DOCUMENTS + 1)]
    sent: list[list[str]] = []

    async def post(batch: IndexingBatch) -> list[IndexingResult]:
        keys = _keys(batch)
        sent.append(keys)
        if len(sent) == 2:
            raise SearchUnavailableError("second batch fell over")
        return [_ok(key) for key in keys]

    results = await run_indexing_with_retry(
        plan_batches, post, documents, IndexingAction.UPSERT, sleep=_no_sleep
    )
    assert len(sent[0]) == MAX_BATCH_DOCUMENTS
    assert sent[1] == ["k1000"]
    # The retry carries only the failed batch's key.
    assert sent[2] == ["k1000"]
    assert len(results) == MAX_BATCH_DOCUMENTS + 1
    assert all(classify(r) is Disposition.SUCCEEDED for r in results.values())


async def test_request_level_exhaustion_yields_retryable_results_not_silence() -> None:
    # Fail closed: with no per-document answer, every outstanding key gets a
    # synthesized retryable result so the gate stays shut.
    async def post(_batch: IndexingBatch) -> list[IndexingResult]:
        raise SearchUnavailableError("still down")

    results = await run_indexing_with_retry(
        plan_batches, post, DOCUMENTS, IndexingAction.UPSERT, sleep=_no_sleep
    )
    assert set(results) == {"a", "b"}
    assert all(classify(r) is Disposition.RETRYABLE for r in results.values())


async def test_attempts_are_bounded() -> None:
    attempts: list[int] = []
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    async def post(_batch: IndexingBatch) -> list[IndexingResult]:
        attempts.append(1)
        raise SearchUnavailableError("down")

    await run_indexing_with_retry(plan_batches, post, DOCUMENTS, IndexingAction.UPSERT, sleep=sleep)
    assert len(attempts) == MAX_INDEXING_ATTEMPTS
    # One retry backs off by `_BACKOFF_SECONDS`, the next by double that — a
    # delay per retry, none before the first attempt.
    assert delays == [_BACKOFF_SECONDS, _BACKOFF_SECONDS * 2]


async def test_an_oversized_document_is_not_retried() -> None:
    # DocumentTooLargeError is local and permanent. Retrying it three times
    # and then rewriting it as a synthesized 503 would destroy the "fail
    # before sending" guarantee the batcher exists to provide.
    async def post(_batch: IndexingBatch) -> list[IndexingResult]:
        raise AssertionError("nothing should be sent")

    with pytest.raises(DocumentTooLargeError):
        await run_indexing_with_retry(
            plan_batches,
            post,
            [{"chunk_id": "a", "content": "x" * (MAX_REQUEST_BYTES + 1)}],
            IndexingAction.UPSERT,
            sleep=_no_sleep,
        )


async def test_two_verdicts_for_one_key_in_one_response_fail_closed() -> None:
    # The contract says one final collection holds one result per key. A
    # response carrying both a transient failure and a success for the same
    # key cannot be resolved — taking the last one would open the gate on a
    # failure that was never actually settled.
    async def post(_batch: IndexingBatch) -> list[IndexingResult]:
        return [_retryable("a"), _ok("a"), _ok("b")]

    # SearchUnavailableError's client-facing str() is the fixed class
    # message; the diagnosable detail lives in upstream_detail instead.
    with pytest.raises(SearchUnavailableError) as exc_info:
        await run_indexing_with_retry(
            plan_batches, post, DOCUMENTS, IndexingAction.UPSERT, sleep=_no_sleep
        )
    assert "repeated a key" in str(exc_info.value.upstream_detail)


async def test_a_rejected_request_is_not_retried() -> None:
    # A 400 on the whole batch means our request is malformed; sending it
    # again cannot help.
    attempts: list[int] = []

    async def post(_batch: IndexingBatch) -> list[IndexingResult]:
        attempts.append(1)
        raise SearchRequestRejectedError("bad batch")

    with pytest.raises(SearchRequestRejectedError):
        await run_indexing_with_retry(
            plan_batches, post, DOCUMENTS, IndexingAction.UPSERT, sleep=_no_sleep
        )
    assert len(attempts) == 1


async def test_a_key_missing_from_every_response_stays_outstanding() -> None:
    # A key that was sent but never mentioned in the response has no verdict.
    # If it were dropped instead of kept outstanding, it would vanish from the
    # result entirely rather than end with a terminal result.
    async def post(batch: IndexingBatch) -> list[IndexingResult]:
        return [_ok(key) for key in _keys(batch) if key != "b"]

    results = await run_indexing_with_retry(
        plan_batches, post, DOCUMENTS, IndexingAction.UPSERT, sleep=_no_sleep
    )
    assert set(results) == {"a", "b"}
    assert classify(results["b"]) is Disposition.RETRYABLE


async def test_a_response_mentioning_a_key_that_was_not_sent_is_rejected() -> None:
    # `may_delete_stale()`'s own duplicate/unexpected-key checks are narrowed
    # at their call site in `DocumentReplacer._replace()` on the assumption
    # that this guard already covers a response naming a key outside the
    # batch — nothing exercises it directly. Without it, a phantom key would
    # enter `terminal`, join `still_outstanding`, and the next attempt would
    # raise an unhandled `KeyError` building `by_key[key]` in the coordinator.
    async def post(_batch: IndexingBatch) -> list[IndexingResult]:
        return [_ok("a"), _ok("b"), _ok("ghost")]

    with pytest.raises(SearchUnavailableError) as exc_info:
        await run_indexing_with_retry(
            plan_batches, post, DOCUMENTS, IndexingAction.UPSERT, sleep=_no_sleep
        )
    assert "ghost" in str(exc_info.value.upstream_detail)


async def test_a_synthesized_error_carries_its_own_batchs_message() -> None:
    # A synthesized result for one batch's key must carry that batch's own
    # error text, not whichever batch the retry loop happened to process
    # last within the same attempt — that text reaches the evidence write-up.
    documents = [{"chunk_id": f"k{i}"} for i in range(MAX_BATCH_DOCUMENTS + 1)]

    async def post(batch: IndexingBatch) -> list[IndexingResult]:
        keys = _keys(batch)
        if len(keys) == MAX_BATCH_DOCUMENTS:
            raise SearchUnavailableError("first batch down")
        raise SearchUnavailableError("second batch down")

    results = await run_indexing_with_retry(
        plan_batches, post, documents, IndexingAction.UPSERT, sleep=_no_sleep
    )
    assert results["k0"].error_message == "first batch down"
    assert results["k1000"].error_message == "second batch down"


async def test_duplicate_keys_in_the_input_are_rejected_before_sending() -> None:
    # Collapsing two documents that share a chunk_id into one upload would
    # silently drop the earlier one and report nothing about it. That must
    # fail closed at the boundary, before anything is sent.
    documents = [{"chunk_id": "dup-1"}, {"chunk_id": "dup-1"}]

    async def post(_batch: IndexingBatch) -> list[IndexingResult]:
        raise AssertionError("nothing should be sent")

    with pytest.raises(DuplicateChunkIdError) as exc_info:
        await run_indexing_with_retry(
            plan_batches, post, documents, IndexingAction.UPSERT, sleep=_no_sleep
        )
    assert "dup-1" in str(exc_info.value.upstream_detail)
