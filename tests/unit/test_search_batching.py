"""Batching guards two ceilings at once and reports which keys each body holds."""

import json

import pytest

from azgenai_lab.services.search_indexing import (
    MAX_BATCH_DOCUMENTS,
    MAX_REQUEST_BYTES,
    DocumentTooLargeError,
    serialize_batches,
)


def _documents(count: int, filler: str = "x") -> list[dict[str, object]]:
    return [{"chunk_id": f"doc-{i:05d}", "content": filler} for i in range(count)]


def test_batch_is_valid_json_with_the_action_attached() -> None:
    batches = list(serialize_batches(_documents(2), "upload"))
    assert len(batches) == 1
    payload = json.loads(batches[0].body)
    assert [d["@search.action"] for d in payload["value"]] == ["upload", "upload"]
    assert batches[0].keys == ("doc-00000", "doc-00001")


def test_document_count_ceiling_is_honoured() -> None:
    batches = list(serialize_batches(_documents(MAX_BATCH_DOCUMENTS + 1), "upload"))
    assert len(batches) == 2
    assert len(batches[0].keys) == MAX_BATCH_DOCUMENTS
    assert len(batches[1].keys) == 1


def test_byte_ceiling_splits_before_the_count_ceiling_does() -> None:
    # Documents carrying vectors hit the 16 MB payload limit well before the
    # 1,000-document limit. Batching by count alone writes a 400.
    batches = list(serialize_batches(_documents(20, filler="y" * 2_000_000), "upload"))
    assert len(batches) > 1
    assert all(len(batch.body) <= MAX_REQUEST_BYTES for batch in batches)


def test_every_emitted_batch_fits_including_wrapper_and_commas() -> None:
    for batch in serialize_batches(_documents(60, filler="z" * 1_000_000), "upload"):
        assert len(batch.body) <= MAX_REQUEST_BYTES
        json.loads(batch.body)  # still parses: the wrapper was accounted for


def test_a_single_oversized_document_fails_before_the_request_is_sent() -> None:
    with pytest.raises(DocumentTooLargeError) as caught:
        list(serialize_batches(_documents(1, filler="q" * (MAX_REQUEST_BYTES + 1)), "upload"))

    # DocumentTooLargeError.message is a fixed, client-facing string (never
    # leaks upstream detail into the HTTP response); the oversized document key
    # belongs in upstream_detail instead, so assert there.
    assert caught.value.upstream_detail is not None
    assert "doc-00000" in caught.value.upstream_detail


def test_keys_partition_the_input_exactly_once() -> None:
    batches = list(serialize_batches(_documents(MAX_BATCH_DOCUMENTS + 5), "upload"))
    seen = [key for batch in batches for key in batch.keys]
    assert seen == [f"doc-{i:05d}" for i in range(MAX_BATCH_DOCUMENTS + 5)]


def test_delete_batches_carry_the_delete_action() -> None:
    batch = next(iter(serialize_batches([{"chunk_id": "a"}], "delete")))
    assert json.loads(batch.body)["value"][0]["@search.action"] == "delete"
