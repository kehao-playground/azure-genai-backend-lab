"""Enumeration pages with a strict cursor and refuses to loop."""

from typing import Any

import pytest

from azgenai_lab.services.search_indexing import EnumerationError, list_indexed_chunk_ids


async def test_single_page_enumeration_sends_the_expected_query() -> None:
    calls: list[dict[str, Any]] = []

    async def post_search(body: dict[str, Any]) -> list[str]:
        calls.append(body)
        return ["doc-0000", "doc-0001"] if len(calls) == 1 else []

    assert await list_indexed_chunk_ids(post_search, "doc") == ["doc-0000", "doc-0001"]
    assert calls[0]["filter"] == "parent_id eq 'doc'"
    assert calls[0]["orderby"] == "chunk_id asc"
    assert calls[0]["search"] == "*"
    assert calls[0]["select"] == "chunk_id"


async def test_second_page_uses_a_strict_greater_than_cursor() -> None:
    # `ge` — which the official paging example uses — repeats the previous
    # page's last key, and re-deriving the cursor from that repeat stops it
    # advancing at all.
    calls: list[dict[str, Any]] = []

    async def post_search(body: dict[str, Any]) -> list[str]:
        calls.append(body)
        if len(calls) == 1:
            return ["doc-0000", "doc-0001"]
        if len(calls) == 2:
            return ["doc-0002"]
        return []

    assert await list_indexed_chunk_ids(post_search, "doc") == [
        "doc-0000",
        "doc-0001",
        "doc-0002",
    ]
    assert calls[1]["filter"] == "parent_id eq 'doc' and chunk_id gt 'doc-0001'"


async def test_a_repeated_key_fails_closed() -> None:
    async def post_search(_body: dict[str, Any]) -> list[str]:
        return ["doc-0000"]

    with pytest.raises(EnumerationError) as exc_info:
        await list_indexed_chunk_ids(post_search, "doc")

    # EnumerationError.message is a fixed, client-facing string; the detail
    # that identifies which key repeated belongs in upstream_detail instead.
    assert exc_info.value.upstream_detail is not None
    assert "repeated" in exc_info.value.upstream_detail


async def test_a_non_advancing_cursor_fails_closed() -> None:
    # Distinct keys throughout, so the repeated-key branch cannot fire: the
    # second page returns an unseen key that sorts *below* the cursor, which
    # only the cursor check can catch.
    calls: list[dict[str, Any]] = []

    async def post_search(body: dict[str, Any]) -> list[str]:
        calls.append(body)
        return ["doc-0005"] if len(calls) == 1 else ["doc-0001"]

    with pytest.raises(EnumerationError) as exc_info:
        await list_indexed_chunk_ids(post_search, "doc")

    assert exc_info.value.upstream_detail is not None
    assert "did not advance" in exc_info.value.upstream_detail


async def test_parent_id_with_a_quote_is_escaped() -> None:
    calls: list[dict[str, Any]] = []

    async def post_search(body: dict[str, Any]) -> list[str]:
        calls.append(body)
        return []

    await list_indexed_chunk_ids(post_search, "o'brien")
    assert calls[0]["filter"] == "parent_id eq 'o''brien'"
