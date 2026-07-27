"""The chunk is the unit of retrieval, of citation, and of billing. Its
invariants are enforced at construction so that no downstream stage has to
re-check them.
"""

from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from azgenai_lab.models.rag import Chunk, SourceDocument, make_chunk_id
from azgenai_lab.models.search_index import DOCUMENT_KEY_MAX_LENGTH, DocumentKeyError


def _document(doc_id: str = "returns-policy") -> SourceDocument:
    return SourceDocument(
        doc_id=doc_id,
        title="Returns Policy",
        doc_type="policy",
        tenant_id="acme",
        effective_date=date(2026, 1, 15),
        body="# Returns Policy\n\nWe accept returns.",
    )


def _chunk(**overrides: object) -> Chunk:
    fields: dict[str, object] = {
        "chunk_id": "returns-policy-0000",
        "parent_id": "returns-policy",
        "title": "Returns Policy",
        "heading_path": "Returns Policy > Refund window",
        "content": "Refunds are issued within 14 days.",
        "doc_type": "policy",
        "tenant_id": "acme",
        "effective_date": date(2026, 1, 15),
    }
    fields.update(overrides)
    return Chunk(**fields)  # type: ignore[arg-type]


def test_chunk_id_is_parent_plus_zero_padded_ordinal() -> None:
    assert make_chunk_id("returns-policy", 0) == "returns-policy-0000"
    assert make_chunk_id("returns-policy", 42) == "returns-policy-0042"


def test_chunk_id_stays_legal_past_four_digits() -> None:
    # Ordinals beyond 9999 widen the suffix. The key stays unique and legal;
    # it just stops being fixed-width.
    assert make_chunk_id("doc", 12345) == "doc-12345"


def test_embedding_input_prefixes_the_heading_path() -> None:
    chunk = _chunk()
    assert chunk.embedding_input == (
        "Returns Policy > Refund window\n\nRefunds are issued within 14 days."
    )


def test_content_is_the_citation_text_and_excludes_the_heading_prefix() -> None:
    # The text we embed and the text we show a reader are deliberately
    # different. This test exists so nobody "fixes" that.
    assert _chunk().content == "Refunds are issued within 14 days."


def test_heading_path_must_begin_with_the_document_title() -> None:
    with pytest.raises(ValueError, match="heading_path"):
        _chunk(heading_path="Refund window")


def test_heading_path_may_be_the_title_alone() -> None:
    assert _chunk(heading_path="Returns Policy").heading_path == "Returns Policy"


def test_title_must_be_a_whole_segment_not_a_character_prefix() -> None:
    # "Return" is a prefix of "Returns Policy" but not a heading segment.
    with pytest.raises(ValueError, match="complete segment"):
        _chunk(title="Return", heading_path="Returns Policy > Exceptions")


def test_illegal_chunk_id_is_rejected_at_construction() -> None:
    with pytest.raises(DocumentKeyError):
        _chunk(chunk_id="returns policy-0000")


def test_maximum_length_doc_id_still_yields_a_legal_chunk_id() -> None:
    # The 1024-character limit binds the derived key, not the authored doc_id.
    parent = "a" * (DOCUMENT_KEY_MAX_LENGTH - len("-0000"))
    chunk_id = make_chunk_id(parent, 0)
    assert len(chunk_id) == DOCUMENT_KEY_MAX_LENGTH
    assert _chunk(chunk_id=chunk_id, parent_id=parent).chunk_id == chunk_id


def test_over_length_derived_chunk_id_is_rejected() -> None:
    parent = "a" * DOCUMENT_KEY_MAX_LENGTH
    with pytest.raises(DocumentKeyError):
        _chunk(chunk_id=make_chunk_id(parent, 0), parent_id=parent)


def test_source_document_is_frozen() -> None:
    document = _document()
    with pytest.raises(FrozenInstanceError):
        document.title = "changed"  # type: ignore[misc]
