"""Replacing a document's chunks is not a transaction.

The upload happens first and the stale chunks are deleted afterwards, so the
worst outcome of a failure is duplicate content rather than a document that
has vanished. That ordering is only safe if the deletion is gated on every
single upload having succeeded, which is what these tests hold in place.
"""

import pytest

from azgenai_lab.services.indexing_results import (
    Disposition,
    IndexingResult,
    classify,
    is_storage_alert,
    may_delete_stale,
)


def _result(key: str, status_code: int) -> IndexingResult:
    return IndexingResult(
        key=key,
        status=status_code in (200, 201),
        status_code=status_code,
        error_message=None if status_code in (200, 201) else "failed",
    )


@pytest.mark.parametrize("status_code", [200, 201])
def test_success_codes(status_code: int) -> None:
    assert classify(_result("a", status_code)) is Disposition.SUCCEEDED


@pytest.mark.parametrize("status_code", [400, 404])
def test_permanent_failures_are_not_retried(status_code: int) -> None:
    assert classify(_result("a", status_code)) is Disposition.PERMANENT


@pytest.mark.parametrize("status_code", [409, 422, 429, 503])
def test_transient_failures_are_retryable(status_code: int) -> None:
    assert classify(_result("a", status_code)) is Disposition.RETRYABLE


def test_unknown_status_is_treated_as_permanent() -> None:
    # An unrecognised code must not silently become a retry loop.
    assert classify(_result("a", 418)) is Disposition.PERMANENT


def test_throttling_raises_a_storage_alert() -> None:
    # 429 during indexing usually means the index is running out of storage,
    # not that the client is sending too fast.
    assert is_storage_alert(_result("a", 429)) is True
    assert is_storage_alert(_result("a", 503)) is False


def test_delete_is_allowed_only_when_every_upload_succeeded() -> None:
    results = [_result("a", 201), _result("b", 200)]

    assert may_delete_stale(results, expected_keys={"a", "b"}) is True


def test_one_permanent_failure_blocks_the_delete() -> None:
    results = [_result("a", 201), _result("b", 400)]

    assert may_delete_stale(results, expected_keys={"a", "b"}) is False


def test_one_retryable_failure_blocks_the_delete() -> None:
    # Retryable means "not finished yet", not "good enough to start deleting".
    results = [_result("a", 201), _result("b", 503)]

    assert may_delete_stale(results, expected_keys={"a", "b"}) is False


def test_a_missing_result_blocks_the_delete() -> None:
    # A 207 response reports on the documents it processed. A key with no
    # result at all is the most dangerous case: it looks like nothing failed.
    assert may_delete_stale([_result("a", 201)], expected_keys={"a", "b"}) is False


def test_an_unexpected_extra_result_blocks_the_delete() -> None:
    results = [_result("a", 201), _result("c", 201)]

    assert may_delete_stale(results, expected_keys={"a"}) is False


def test_no_expected_keys_means_nothing_to_delete_against() -> None:
    assert may_delete_stale([], expected_keys=set()) is False


def test_status_false_beats_a_success_code() -> None:
    # status is the service's own success flag. A contradictory pair is read as
    # a failure, never as a success.
    contradictory = IndexingResult(key="a", status=False, status_code=201)

    assert classify(contradictory) is Disposition.PERMANENT
    assert may_delete_stale([contradictory], expected_keys={"a"}) is False


def test_a_duplicate_key_blocks_the_delete() -> None:
    # A later success for the same key must not paper over an earlier failure.
    results = [_result("a", 503), _result("a", 201)]

    assert may_delete_stale(results, expected_keys={"a"}) is False


def test_a_duplicate_key_blocks_the_delete_even_if_every_result_succeeded() -> None:
    # Isolates the dedup check from the success check above: two *successful*
    # results for the same key must still block, since a batch is only
    # supposed to report on each document once and a repeat is itself a sign
    # something about the response cannot be trusted.
    results = [_result("a", 201), _result("a", 200)]

    assert may_delete_stale(results, expected_keys={"a"}) is False
