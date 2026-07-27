"""Chunking is pure: same document and same parameters, same chunks.

The unit of a chunk is a Markdown section, because a section boundary is a
boundary a reader recognises — which is what makes a citation useful.
"""

from collections import Counter
from datetime import date

import pytest

from azgenai_lab.models.rag import Chunk, SourceDocument
from azgenai_lab.services.chunking import ChunkingError, chunk_markdown
from azgenai_lab.services.document_loader import load_documents


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


def _is_subsequence(needle: str, haystack: str) -> bool:
    iterator = iter(haystack)
    return all(character in iterator for character in needle)


def _squeeze(text: str) -> str:
    return "".join(text.split())


LONG_SENTENCES = " ".join(f"Sentence number {index} is here." for index in range(200))


def test_oversized_section_splits_into_several_chunks() -> None:
    body = f"# Returns Policy\n\n## Exceptions\n\n{LONG_SENTENCES}\n"
    chunks = chunk_markdown(_document(body), max_chars=500, overlap_chars=100)

    assert len(chunks) > 1
    assert all(chunk.heading_path == "Returns Policy > Exceptions" for chunk in chunks)


def test_every_embedding_input_respects_the_maximum() -> None:
    body = f"# Returns Policy\n\n## Exceptions\n\n{LONG_SENTENCES}\n"
    for chunk in chunk_markdown(_document(body), max_chars=500, overlap_chars=100):
        assert len(chunk.embedding_input) <= 500


def test_no_character_of_the_source_is_lost() -> None:
    body = f"# Returns Policy\n\n## Exceptions\n\n{LONG_SENTENCES}\n"
    chunks = chunk_markdown(_document(body), max_chars=500, overlap_chars=100)

    joined = _squeeze("".join(chunk.content for chunk in chunks))
    assert _is_subsequence(_squeeze(LONG_SENTENCES), joined)


def test_consecutive_chunks_of_one_section_overlap() -> None:
    body = f"# Returns Policy\n\n## Exceptions\n\n{LONG_SENTENCES}\n"
    chunks = chunk_markdown(_document(body), max_chars=500, overlap_chars=100)

    first, second = chunks[0], chunks[1]
    tail = second.content[: len(second.content) - len(second.content.lstrip())]
    del tail
    # The second chunk opens with text that also appears at the end of the first.
    opening = second.content[:40]
    assert opening in first.content


def test_chunks_from_different_sections_never_overlap() -> None:
    body = (
        "# Returns Policy\n\n"
        "## Refund window\n\nAlpha sentence one. Alpha sentence two.\n\n"
        "## Exceptions\n\nBravo sentence one. Bravo sentence two.\n"
    )
    chunks = chunk_markdown(_document(body), max_chars=2000, overlap_chars=500)

    assert len(chunks) == 2
    assert "Alpha" not in chunks[1].content
    assert "Bravo" not in chunks[0].content


def test_overlap_is_sentence_aligned() -> None:
    body = f"# Returns Policy\n\n## Exceptions\n\n{LONG_SENTENCES}\n"
    chunks = chunk_markdown(_document(body), max_chars=500, overlap_chars=100)

    # A sentence-aligned tail begins a sentence, so the second chunk does not
    # open mid-word.
    assert chunks[1].content.startswith("Sentence number")


def test_zero_overlap_produces_no_repetition() -> None:
    body = f"# Returns Policy\n\n## Exceptions\n\n{LONG_SENTENCES}\n"
    chunks = chunk_markdown(_document(body), max_chars=500, overlap_chars=0)

    joined = _squeeze("".join(chunk.content for chunk in chunks))
    assert joined == _squeeze(LONG_SENTENCES)


def test_cjk_sentences_split_without_spaces() -> None:
    # Chinese text has no inter-word spaces and its own terminators. A splitter
    # that assumes whitespace word boundaries silently produces one huge chunk.
    sentence = "退貨規定如下。"
    body = "# Returns Policy\n\n## Exceptions\n\n" + sentence * 200 + "\n"
    chunks = chunk_markdown(_document(body), max_chars=300, overlap_chars=50)

    assert len(chunks) > 1
    assert all(len(chunk.embedding_input) <= 300 for chunk in chunks)
    assert all(chunk.content.endswith("。") for chunk in chunks)


def test_a_single_unbreakable_run_is_hard_split() -> None:
    body = "# Returns Policy\n\n## Exceptions\n\n" + "x" * 4000 + "\n"
    chunks = chunk_markdown(_document(body), max_chars=500, overlap_chars=100)

    assert len(chunks) > 1
    assert all(len(chunk.embedding_input) <= 500 for chunk in chunks)


def test_overlap_leaving_no_room_to_advance_is_an_error() -> None:
    body = "# Returns Policy\n\n## Exceptions\n\n" + "word " * 500 + "\n"
    with pytest.raises(ChunkingError, match="overlap"):
        chunk_markdown(_document(body), max_chars=60, overlap_chars=50)


def _corpus_chunks() -> list[Chunk]:
    return [
        chunk
        for document in load_documents()
        for chunk in chunk_markdown(document, max_chars=2000, overlap_chars=500)
    ]


def test_the_shipped_corpus_chunks_cleanly() -> None:
    chunks = _corpus_chunks()

    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)
    assert all(len(chunk.embedding_input) <= 2000 for chunk in chunks)


def test_the_corpus_exercises_the_oversize_path() -> None:
    # A chunk count alone proves nothing: four leaf sections plus a root
    # introduction already make five chunks without anything being split. The
    # only real evidence of splitting is two chunks sharing one heading path.
    paths = Counter(chunk.heading_path for chunk in _corpus_chunks())
    split_paths = [path for path, count in paths.items() if count > 1]

    assert split_paths, "no section in the corpus is long enough to be split"
    assert any(path.startswith("Returns Policy") for path in split_paths)


def test_the_corpus_exercises_heading_depth() -> None:
    depths = {chunk.heading_path.count(" > ") for chunk in _corpus_chunks()}

    assert max(depths) >= 2, "no document uses a '###' subsection"


def test_the_corpus_is_split_across_two_tenants() -> None:
    # Day 15 filters on tenant_id; a single-tenant corpus cannot demonstrate it.
    tenants = Counter(document.tenant_id for document in load_documents())

    assert tenants == {"acme": 2, "globex": 2}
