"""Chunking is pure: same document and same parameters, same chunks.

The unit of a chunk is a Markdown section, because a section boundary is a
boundary a reader recognises — which is what makes a citation useful.
"""

from datetime import date

import pytest

from azgenai_lab.models.rag import SourceDocument
from azgenai_lab.services.chunking import ChunkingError, chunk_markdown


def _document(body: str) -> SourceDocument:
    return SourceDocument(
        doc_id="returns-policy",
        title="Returns Policy",
        doc_type="policy",
        tenant_id="acme",
        effective_date=date(2026, 1, 15),
        body=body,
    )


BODY = """# Returns Policy

Scope paragraph.

## Refund window

### Standard purchases

Refunds within fourteen days.

### Promotional purchases

Refunds within seven days.

## Exceptions

Perishable goods are excluded.
"""


def test_each_in_budget_section_becomes_one_chunk() -> None:
    chunks = chunk_markdown(_document(BODY), max_chars=2000, overlap_chars=500)

    assert [chunk.heading_path for chunk in chunks] == [
        "Returns Policy",
        "Returns Policy > Refund window > Standard purchases",
        "Returns Policy > Refund window > Promotional purchases",
        "Returns Policy > Exceptions",
    ]


def test_heading_path_always_starts_with_the_document_title() -> None:
    for chunk in chunk_markdown(_document(BODY), max_chars=2000, overlap_chars=500):
        assert chunk.heading_path.startswith("Returns Policy")


def test_chunk_ids_are_sequential_and_parented() -> None:
    chunks = chunk_markdown(_document(BODY), max_chars=2000, overlap_chars=500)

    assert [chunk.chunk_id for chunk in chunks[:2]] == [
        "returns-policy-0000",
        "returns-policy-0001",
    ]
    assert all(chunk.parent_id == "returns-policy" for chunk in chunks)


def test_metadata_is_copied_onto_every_chunk() -> None:
    for chunk in chunk_markdown(_document(BODY), max_chars=2000, overlap_chars=500):
        assert chunk.tenant_id == "acme"
        assert chunk.doc_type == "policy"
        assert chunk.effective_date == date(2026, 1, 15)


def test_headings_are_not_repeated_inside_the_content() -> None:
    chunks = chunk_markdown(_document(BODY), max_chars=2000, overlap_chars=500)
    exceptions = next(c for c in chunks if c.heading_path.endswith("Exceptions"))

    assert exceptions.content == "Perishable goods are excluded."
    assert "##" not in exceptions.content


def test_sections_with_no_prose_are_dropped() -> None:
    body = "# Returns Policy\n\n## Empty\n\n## Real\n\nText here.\n"
    chunks = chunk_markdown(_document(body), max_chars=2000, overlap_chars=500)

    assert [chunk.heading_path for chunk in chunks] == ["Returns Policy > Real"]


def test_body_without_headings_yields_a_single_title_chunk() -> None:
    chunks = chunk_markdown(_document("Just prose."), max_chars=2000, overlap_chars=500)

    assert len(chunks) == 1
    assert chunks[0].heading_path == "Returns Policy"
    assert chunks[0].content == "Just prose."


def test_chunking_is_deterministic() -> None:
    first = chunk_markdown(_document(BODY), max_chars=2000, overlap_chars=500)
    second = chunk_markdown(_document(BODY), max_chars=2000, overlap_chars=500)

    assert first == second


def test_embedding_input_never_exceeds_the_maximum() -> None:
    for chunk in chunk_markdown(_document(BODY), max_chars=2000, overlap_chars=500):
        assert len(chunk.embedding_input) <= 2000


def test_heading_path_too_long_for_the_budget_is_an_error() -> None:
    body = "# Returns Policy\n\n## " + "x" * 300 + "\n\nText.\n"
    with pytest.raises(ChunkingError, match="heading path"):
        chunk_markdown(_document(body), max_chars=320, overlap_chars=100)


def test_small_prose_fits_even_when_budget_is_at_or_below_overlap() -> None:
    # heading_path is "Returns Policy > " + "H" * 77 (94 chars); budget is
    # 100 - 94 - len("\n\n") == 4, which is below overlap_chars=10. The guard
    # must not fire on that arithmetic alone: "Hi" (2 chars) fits comfortably
    # inside budget=4, so no split is ever needed for this section.
    body = "# Returns Policy\n\n## " + "H" * 77 + "\n\nHi\n"
    chunks = chunk_markdown(_document(body), max_chars=100, overlap_chars=10)

    assert len(chunks) == 1
    assert chunks[0].content == "Hi"


def test_heading_level_skip_produces_a_correct_heading_path() -> None:
    # "## Section" has no prose of its own before "#### Detail" skips a
    # level (no "###" in between); it is dropped like any other prose-less
    # heading, but the stack still records it so it survives as a breadcrumb
    # segment in "Detail"'s heading_path.
    body = "# Returns Policy\n\n## Section\n\n#### Detail\n\nDeep content.\n"
    chunks = chunk_markdown(_document(body), max_chars=2000, overlap_chars=500)

    assert [chunk.heading_path for chunk in chunks] == [
        "Returns Policy > Section > Detail",
    ]
