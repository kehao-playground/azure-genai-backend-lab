"""Enumeration walks with a strict cursor and refuses to loop.

What a page *is* — a filter, an ordering, a page size — belongs to the
adapter; `tests/unit/test_search_data_plane.py` pins that. These drive the
walk itself, so the two arguments it asks each page for are the whole surface
under test here.
"""

import pytest

from azgenai_lab.services.search_indexing import EnumerationError, list_indexed_chunk_ids


async def test_single_page_enumeration_asks_for_the_document_from_the_start() -> None:
    calls: list[tuple[str, str | None]] = []

    async def list_page(parent_id: str, after: str | None) -> list[str]:
        calls.append((parent_id, after))
        return ["doc-0000", "doc-0001"] if len(calls) == 1 else []

    assert await list_indexed_chunk_ids(list_page, "doc") == ["doc-0000", "doc-0001"]
    assert calls[0] == ("doc", None)


async def test_the_second_page_resumes_strictly_after_the_last_key() -> None:
    # Resuming *at* the last key — what the official paging example does —
    # repeats it, and re-deriving the cursor from that repeat stops the walk
    # advancing at all.
    calls: list[tuple[str, str | None]] = []

    async def list_page(parent_id: str, after: str | None) -> list[str]:
        calls.append((parent_id, after))
        if len(calls) == 1:
            return ["doc-0000", "doc-0001"]
        if len(calls) == 2:
            return ["doc-0002"]
        return []

    assert await list_indexed_chunk_ids(list_page, "doc") == [
        "doc-0000",
        "doc-0001",
        "doc-0002",
    ]
    assert calls[1] == ("doc", "doc-0001")


async def test_a_repeated_key_fails_closed() -> None:
    async def list_page(_parent_id: str, _after: str | None) -> list[str]:
        return ["doc-0000"]

    with pytest.raises(EnumerationError) as exc_info:
        await list_indexed_chunk_ids(list_page, "doc")

    # EnumerationError.message is a fixed, client-facing string; the detail
    # that identifies which key repeated belongs in upstream_detail instead.
    assert exc_info.value.upstream_detail is not None
    assert "repeated" in exc_info.value.upstream_detail


async def test_a_non_advancing_cursor_fails_closed() -> None:
    # Distinct keys throughout, so the repeated-key branch cannot fire: the
    # second page returns an unseen key that sorts *below* the cursor, which
    # only the cursor check can catch.
    calls: list[tuple[str, str | None]] = []

    async def list_page(parent_id: str, after: str | None) -> list[str]:
        calls.append((parent_id, after))
        return ["doc-0005"] if len(calls) == 1 else ["doc-0001"]

    with pytest.raises(EnumerationError) as exc_info:
        await list_indexed_chunk_ids(list_page, "doc")

    assert exc_info.value.upstream_detail is not None
    assert "did not advance" in exc_info.value.upstream_detail


async def test_the_parent_id_travels_verbatim() -> None:
    # Quoting is the adapter's job. Escaping here as well would double it, and
    # a walk that quietly enumerated a different document than the one it was
    # asked about would delete the wrong chunks.
    calls: list[tuple[str, str | None]] = []

    async def list_page(parent_id: str, after: str | None) -> list[str]:
        calls.append((parent_id, after))
        return []

    await list_indexed_chunk_ids(list_page, "o'brien")
    assert calls[0] == ("o'brien", None)
