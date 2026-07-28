"""Replacement is upload -> gate -> enumerate -> delete, inside a lock."""

import asyncio
import json
from typing import Any

import pytest

from azgenai_lab.services.indexing_results import IndexingResult
from azgenai_lab.services.search_indexing import DocumentReplacer, ReplacementOutcome


async def _no_sleep(_seconds: float) -> None:
    return None


def _ok(key: str) -> IndexingResult:
    return IndexingResult(key=key, status=True, status_code=200)


def _retryable(key: str) -> IndexingResult:
    return IndexingResult(key=key, status=False, status_code=503, error_message="busy")


class _Index:
    """An in-memory stand-in for the service, faithful about parent scoping."""

    def __init__(self, existing: dict[str, str] | None = None) -> None:
        # chunk_id -> parent_id, so enumeration can honour the filter the way
        # the real service does. A stand-in that ignores parent_id would hide
        # cross-document deletion entirely.
        self.keys: dict[str, str] = dict(existing or {})
        self.delete_results: dict[str, list[IndexingResult]] = {}
        self.on_search: Any = None

    async def post_index(self, body: bytes) -> list[IndexingResult]:
        payload = json.loads(body)
        results: list[IndexingResult] = []
        for document in payload["value"]:
            key = document["chunk_id"]
            if document["@search.action"] == "upload":
                self.keys[key] = document.get("parent_id", "")
                results.append(_ok(key))
                continue
            scripted = self.delete_results.get(key)
            if scripted:
                result = scripted.pop(0)
                if result.status:
                    self.keys.pop(key, None)
                results.append(result)
                continue
            self.keys.pop(key, None)
            results.append(_ok(key))
        return results

    async def post_search(self, body: dict[str, Any]) -> list[str]:
        if self.on_search is not None:
            await self.on_search()
        expression = body["filter"]
        parent = expression.split("parent_id eq '")[1].split("'")[0]
        cursor = None
        if " and chunk_id gt '" in expression:
            cursor = expression.split(" and chunk_id gt '")[1].rstrip("'")
        matching = sorted(
            key
            for key, owner in self.keys.items()
            if owner == parent and (cursor is None or key > cursor)
        )
        return matching[: body["top"]]


def _documents(keys: list[str], parent: str = "doc") -> list[dict[str, Any]]:
    return [{"chunk_id": key, "parent_id": parent, "content": key} for key in keys]


async def test_successful_replacement_deletes_only_stale_chunks() -> None:
    index = _Index({"doc-0000": "doc", "doc-0001": "doc", "doc-0002": "doc"})
    replacer = DocumentReplacer(index.post_index, index.post_search, sleep=_no_sleep)

    outcome = await replacer.replace("doc", _documents(["doc-0000", "doc-0001"]))

    assert outcome.completed is True
    assert outcome.deleted_keys == ("doc-0002",)
    assert outcome.unresolved_stale_ids == ()
    assert sorted(index.keys) == ["doc-0000", "doc-0001"]


async def test_a_blocked_gate_deletes_nothing() -> None:
    index = _Index({"doc-0000": "doc", "doc-0002": "doc"})

    async def failing_upload(_body: bytes) -> list[IndexingResult]:
        return [_retryable("doc-0000")]

    replacer = DocumentReplacer(failing_upload, index.post_search, sleep=_no_sleep)
    outcome = await replacer.replace("doc", _documents(["doc-0000"]))

    assert outcome.completed is False
    assert outcome.deleted_keys == ()
    assert "doc-0002" in index.keys


async def test_a_failed_deletion_reports_unresolved_stale_ids() -> None:
    # Deletion failure is the safe side — stale content survives rather than
    # new content vanishing — but safe is not the same as done.
    index = _Index({"doc-0000": "doc", "doc-0002": "doc"})
    index.delete_results["doc-0002"] = [_retryable("doc-0002")] * 5

    replacer = DocumentReplacer(index.post_index, index.post_search, sleep=_no_sleep)
    outcome = await replacer.replace("doc", _documents(["doc-0000"]))

    assert outcome.completed is False
    assert outcome.unresolved_stale_ids == ("doc-0002",)
    assert "doc-0002" in index.keys


async def test_a_deletion_that_succeeds_on_retry_completes() -> None:
    index = _Index({"doc-0000": "doc", "doc-0002": "doc"})
    index.delete_results["doc-0002"] = [_retryable("doc-0002"), _ok("doc-0002")]

    replacer = DocumentReplacer(index.post_index, index.post_search, sleep=_no_sleep)
    outcome = await replacer.replace("doc", _documents(["doc-0000"]))

    assert outcome.completed is True
    assert outcome.unresolved_stale_ids == ()
    assert "doc-0002" not in index.keys


async def test_nothing_to_delete_still_completes() -> None:
    index = _Index()
    replacer = DocumentReplacer(index.post_index, index.post_search, sleep=_no_sleep)
    outcome = await replacer.replace("doc", _documents(["doc-0000"]))
    assert outcome.completed is True
    assert outcome.deleted_keys == ()


async def test_documents_belonging_to_another_parent_are_refused_before_sending() -> None:
    # Without this check the upload succeeds, the gate passes on the uploaded
    # keys, and then every chunk of the *named* parent is judged stale and
    # deleted — a caller wiring mistake escalating straight to data loss.
    index = _Index({"parent-a-0000": "parent-a"})
    sent: list[bytes] = []

    async def recording_post(body: bytes) -> list[IndexingResult]:
        sent.append(body)
        return await index.post_index(body)

    replacer = DocumentReplacer(recording_post, index.post_search, sleep=_no_sleep)
    with pytest.raises(ValueError, match="parent_id"):
        await replacer.replace("parent-a", _documents(["b-0000"], parent="parent-b"))

    assert sent == []
    assert "parent-a-0000" in index.keys


async def test_one_document_admits_only_one_replacement_at_a_time() -> None:
    # Measured directly rather than inferred from surviving keys: a
    # key-set assertion accepts the corrupted outcome, because A deleting B's
    # new chunk leaves exactly the key set A's own generation would.
    index = _Index()
    replacer = DocumentReplacer(index.post_index, index.post_search, sleep=_no_sleep)

    async def yield_control() -> None:
        # Force a suspension point inside the critical section so two
        # unlocked jobs would certainly interleave here.
        await asyncio.sleep(0)

    index.on_search = yield_control

    await asyncio.gather(
        replacer.replace("doc", _documents(["doc-0000", "doc-0001"])),
        replacer.replace("doc", _documents(["doc-0000", "doc-0001", "doc-0002"])),
    )
    assert replacer.max_concurrent_per_parent == 1


async def test_different_documents_are_not_serialized_against_each_other() -> None:
    index = _Index()
    replacer = DocumentReplacer(index.post_index, index.post_search, sleep=_no_sleep)

    outcomes = await asyncio.gather(
        replacer.replace("a", _documents(["a-0000"], parent="a")),
        replacer.replace("b", _documents(["b-0000"], parent="b")),
    )
    assert all(isinstance(o, ReplacementOutcome) and o.completed for o in outcomes)
    # Neither job may treat the other document's chunks as stale.
    assert sorted(index.keys) == ["a-0000", "b-0000"]
