"""Replacement is upload -> gate -> enumerate -> delete, inside a lock."""

import asyncio
import json
from typing import Any

import pytest

from azgenai_lab.services.azure_search import SearchRequestRejectedError, SearchUnavailableError
from azgenai_lab.services.indexing_results import Disposition, IndexingResult, classify
from azgenai_lab.services.search_data_plane import ENUMERATION_PAGE_SIZE
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
        self.enumerations: list[tuple[str, str | None]] = []

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
                if classify(result) is Disposition.SUCCEEDED:
                    self.keys.pop(key, None)
                results.append(result)
                continue
            self.keys.pop(key, None)
            results.append(_ok(key))
        return results

    async def list_chunk_ids(self, parent_id: str, after: str | None = None) -> list[str]:
        if self.on_search is not None:
            await self.on_search()
        self.enumerations.append((parent_id, after))
        return sorted(
            key
            for key, owner in self.keys.items()
            if owner == parent_id and (after is None or key > after)
        )[:ENUMERATION_PAGE_SIZE]


def _documents(keys: list[str], parent: str = "doc") -> list[dict[str, Any]]:
    return [{"chunk_id": key, "parent_id": parent, "content": key} for key in keys]


async def test_successful_replacement_deletes_only_stale_chunks() -> None:
    index = _Index({"doc-0000": "doc", "doc-0001": "doc", "doc-0002": "doc"})
    replacer = DocumentReplacer(index.post_index, index.list_chunk_ids, sleep=_no_sleep)

    outcome = await replacer.replace("doc", _documents(["doc-0000", "doc-0001"]))

    assert outcome.completed is True
    assert outcome.deleted_keys == ("doc-0002",)
    assert outcome.unresolved_stale_ids == ()
    assert sorted(index.keys) == ["doc-0000", "doc-0001"]


async def test_a_blocked_gate_deletes_nothing() -> None:
    index = _Index({"doc-0000": "doc", "doc-0002": "doc"})

    async def failing_upload(_body: bytes) -> list[IndexingResult]:
        return [_retryable("doc-0000")]

    replacer = DocumentReplacer(failing_upload, index.list_chunk_ids, sleep=_no_sleep)
    outcome = await replacer.replace("doc", _documents(["doc-0000"]))

    assert outcome.completed is False
    assert outcome.deleted_keys == ()
    assert "doc-0002" in index.keys


async def test_a_failed_deletion_reports_unresolved_stale_ids() -> None:
    # Deletion failure is the safe side — stale content survives rather than
    # new content vanishing — but safe is not the same as done.
    index = _Index({"doc-0000": "doc", "doc-0002": "doc"})
    index.delete_results["doc-0002"] = [_retryable("doc-0002")] * 5

    replacer = DocumentReplacer(index.post_index, index.list_chunk_ids, sleep=_no_sleep)
    outcome = await replacer.replace("doc", _documents(["doc-0000"]))

    assert outcome.completed is False
    assert outcome.unresolved_stale_ids == ("doc-0002",)
    assert "doc-0002" in index.keys


async def test_a_deletion_that_succeeds_on_retry_completes() -> None:
    index = _Index({"doc-0000": "doc", "doc-0002": "doc"})
    index.delete_results["doc-0002"] = [_retryable("doc-0002"), _ok("doc-0002")]

    replacer = DocumentReplacer(index.post_index, index.list_chunk_ids, sleep=_no_sleep)
    outcome = await replacer.replace("doc", _documents(["doc-0000"]))

    assert outcome.completed is True
    assert outcome.unresolved_stale_ids == ()
    assert "doc-0002" not in index.keys


async def test_first_replacement_for_a_parent_completes_without_deleting() -> None:
    # Not a test of the `if not stale` early return in `_replace()`: with an
    # empty `stale` list, `run_indexing_with_retry([], "delete", ...)` is
    # already a no-op (`serialize_batches` yields no batches for an empty
    # sequence), so the outcome is identical whether or not that branch
    # exists. What this pins instead is the outward behaviour on a parent's
    # very first upload — no prior chunks exist, so nothing is stale and the
    # replacement completes with an empty deletion.
    index = _Index()
    replacer = DocumentReplacer(index.post_index, index.list_chunk_ids, sleep=_no_sleep)
    outcome = await replacer.replace("doc", _documents(["doc-0000"]))
    assert outcome.completed is True
    assert outcome.deleted_keys == ()


async def test_enumeration_crash_after_upload_reports_unknown_stale_state() -> None:
    # The upload already landed and is live. A crash while walking the index
    # for stale chunks must not read like nothing happened — escaping as a
    # raised exception would do exactly that. The outcome type is the only
    # way to say "new chunks are in, old ones might still be there too, and
    # we cannot say".
    index = _Index({"doc-0000": "doc"})

    async def crashing_search(_parent_id: str, _after: str | None) -> list[str]:
        raise SearchUnavailableError("search timed out")

    replacer = DocumentReplacer(index.post_index, crashing_search, sleep=_no_sleep)
    outcome = await replacer.replace("doc", _documents(["doc-0001"]))

    assert outcome.completed is False
    assert outcome.stale_state_unknown is True
    assert outcome.unresolved_stale_ids == ()
    assert all(r.status for r in outcome.uploaded)
    # The new chunk is live even though cleanup crashed right after.
    assert "doc-0001" in index.keys


async def test_deletion_rejection_after_upload_reports_unknown_stale_state() -> None:
    # A permanent (non-retryable) failure on the delete call — not just a
    # retryable `SearchUnavailableError` — must be caught here too: it leaves
    # the same "upload landed, cleanup outcome unknown" ambiguity.
    index = _Index({"doc-0000": "doc", "doc-0002": "doc"})

    async def rejecting_post(body: bytes) -> list[IndexingResult]:
        payload = json.loads(body)
        if payload["value"][0]["@search.action"] == "delete":
            raise SearchRequestRejectedError("malformed delete batch")
        return await index.post_index(body)

    replacer = DocumentReplacer(rejecting_post, index.list_chunk_ids, sleep=_no_sleep)
    outcome = await replacer.replace("doc", _documents(["doc-0000"]))

    assert outcome.completed is False
    assert outcome.stale_state_unknown is True
    assert "doc-0000" in index.keys  # the upload landed


async def test_replacing_with_no_chunks_is_refused_before_sending() -> None:
    index = _Index({"doc-0000": "doc"})
    sent: list[bytes] = []

    async def recording_post(body: bytes) -> list[IndexingResult]:
        sent.append(body)
        return await index.post_index(body)

    replacer = DocumentReplacer(recording_post, index.list_chunk_ids, sleep=_no_sleep)
    with pytest.raises(ValueError, match="no chunks"):
        await replacer.replace("doc", [])

    # Documents the "before sending" claim rather than testing it: with the
    # guard removed, an empty document sequence still sends nothing, because
    # `serialize_batches` yields no batches for an empty sequence. The
    # `pytest.raises` above is what this test actually pins.
    assert sent == []
    assert "doc-0000" in index.keys


async def test_documents_belonging_to_another_parent_are_refused_before_sending() -> None:
    # Without this check the upload succeeds, the gate passes on the uploaded
    # keys, and then every chunk of the *named* parent is judged stale and
    # deleted — a caller wiring mistake escalating straight to data loss.
    index = _Index({"parent-a-0000": "parent-a"})
    sent: list[bytes] = []

    async def recording_post(body: bytes) -> list[IndexingResult]:
        sent.append(body)
        return await index.post_index(body)

    replacer = DocumentReplacer(recording_post, index.list_chunk_ids, sleep=_no_sleep)
    with pytest.raises(ValueError, match="parent_id"):
        await replacer.replace("parent-a", _documents(["b-0000"], parent="parent-b"))

    assert sent == []
    assert "parent-a-0000" in index.keys


async def test_one_document_admits_only_one_replacement_at_a_time() -> None:
    # Measured directly rather than inferred from surviving keys: a
    # key-set assertion accepts the corrupted outcome, because A deleting B's
    # new chunk leaves exactly the key set A's own generation would.
    index = _Index()
    replacer = DocumentReplacer(index.post_index, index.list_chunk_ids, sleep=_no_sleep)

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
    # `max_concurrent_per_parent` cannot see this: it is keyed by parent_id,
    # so it stays at 1 for each of "a" and "b" regardless of whether the lock
    # is scoped per-parent or is a single lock shared by every parent_id — a
    # regression to one global lock still serializes "a" and "b" but leaves
    # that counter looking identical. Only `max_concurrent_replacements`,
    # which is not keyed by anything, can tell the two apart.
    index = _Index()
    replacer = DocumentReplacer(index.post_index, index.list_chunk_ids, sleep=_no_sleep)

    async def yield_control() -> None:
        # Force a suspension point inside the critical section so two
        # unlocked jobs would certainly interleave here.
        await asyncio.sleep(0)

    index.on_search = yield_control

    outcomes = await asyncio.gather(
        replacer.replace("a", _documents(["a-0000"], parent="a")),
        replacer.replace("b", _documents(["b-0000"], parent="b")),
    )
    assert all(isinstance(o, ReplacementOutcome) and o.completed for o in outcomes)
    # Neither job may treat the other document's chunks as stale.
    assert sorted(index.keys) == ["a-0000", "b-0000"]
    assert replacer.max_concurrent_replacements == 2


async def test_two_parents_pin_per_parent_and_overall_concurrency_together() -> None:
    # Two jobs race the same document, "doc"; a third races "other". Neither
    # counter alone tells the two-job cases above apart from a regression:
    # with only same-parent jobs, `max_concurrent_replacements` cannot be
    # told from a global lock's behaviour without a second, differently
    # scoped parent in flight; with only different-parent jobs,
    # `max_concurrent_per_parent` stays 1 whether the lock is missing,
    # global, or genuinely per-parent, because it is keyed by parent_id and
    # never sees two same-parent jobs overlap. Three jobs mixing parents is
    # the smallest case where both properties are exercised at once: the
    # "doc" jobs must never overlap each other, while the "other" job must
    # overlap one of them.
    index = _Index()
    replacer = DocumentReplacer(index.post_index, index.list_chunk_ids, sleep=_no_sleep)

    async def yield_control() -> None:
        # Force a suspension point inside the critical section so two
        # unlocked jobs would certainly interleave here.
        await asyncio.sleep(0)

    index.on_search = yield_control

    outcomes = await asyncio.gather(
        replacer.replace("doc", _documents(["doc-0000", "doc-0001"])),
        replacer.replace("doc", _documents(["doc-0000", "doc-0001", "doc-0002"])),
        replacer.replace("other", _documents(["other-0000"], parent="other")),
    )
    assert all(isinstance(o, ReplacementOutcome) and o.completed for o in outcomes)
    assert replacer.max_concurrent_per_parent == 1
    assert replacer.max_concurrent_replacements == 2
