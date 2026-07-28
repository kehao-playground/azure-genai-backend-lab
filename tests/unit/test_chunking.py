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


TAB_INDENTED_BLOCK = "\t\tdef handler():\n\t\t    return 1"


def test_the_fast_path_preserves_interior_whitespace() -> None:
    # A section that fits its budget is never handed to `_split_section`'s
    # paragraph-stripping code, so whitespace-significant content in the
    # *interior* of the section — here, tab-indented code — survives exactly
    # as written. The section's own edges are a separate matter: see
    # `test_a_section_is_stripped_at_its_edges_on_both_paths`.
    body = (
        "# Returns Policy\n\n## Exceptions\n\n"
        "See the sample handler below.\n\n"
        f"{TAB_INDENTED_BLOCK}\n\n"
        "End of example.\n"
    )
    chunks = chunk_markdown(_document(body), max_chars=4000, overlap_chars=500)

    assert len(chunks) == 1
    assert TAB_INDENTED_BLOCK in chunks[0].content


def test_a_section_is_stripped_at_its_edges_on_both_paths() -> None:
    # `_sections` strips each section as a whole before `_split_section` ever
    # sees it, so whitespace at the very start or end of a section is gone on
    # BOTH paths. Pinned here because the honest version of the whitespace
    # contract is easy to overstate: no path returns a byte-identical copy of
    # the source, not even the fast one.
    body = "# Returns Policy\n\n## Exceptions\n\n\t\tindented first line\n\nplain second.   \n"
    chunks = chunk_markdown(_document(body), max_chars=4000, overlap_chars=500)

    assert len(chunks) == 1
    content = chunks[0].content
    assert content.startswith("indented first line")
    assert not content.startswith("\t")
    assert content.endswith("plain second.")


def test_the_split_path_strips_paragraph_leading_whitespace() -> None:
    # This documents a known, accepted loss — it is not an endorsement. Once
    # a section must be split, every paragraph is `.strip()`-ed before being
    # repacked (see `_split_section`'s docstring), so a paragraph's leading
    # whitespace does not survive. This makes the split path unsuitable for
    # whitespace-significant material such as indented code or tables; only
    # the fast path (`test_the_fast_path_preserves_interior_whitespace`)
    # keeps interior whitespace.
    filler_before = "Filler prose before the example. " * 6
    filler_after = "Filler prose after the example continues on. " * 6
    body = (
        "# Returns Policy\n\n## Exceptions\n\n"
        f"{filler_before.strip()}\n\n{TAB_INDENTED_BLOCK}\n\n{filler_after.strip()}\n"
    )
    chunks = chunk_markdown(_document(body), max_chars=250, overlap_chars=60)

    assert len(chunks) > 1
    assert not any(TAB_INDENTED_BLOCK in chunk.content for chunk in chunks)
    stripped_form = "def handler():\n\t\t    return 1"
    assert any(stripped_form in chunk.content for chunk in chunks)


def test_runs_of_blank_lines_collapse_on_the_split_path() -> None:
    paragraphs = [
        " ".join(
            f"Sentence number {index} is here." for index in range(group * 20, group * 20 + 20)
        )
        for group in range(5)
    ]
    body = "# Returns Policy\n\n## Exceptions\n\n" + "\n\n\n\n".join(paragraphs) + "\n"
    chunks = chunk_markdown(_document(body), max_chars=500, overlap_chars=100)

    assert len(chunks) > 1
    assert not any("\n\n\n" in chunk.content for chunk in chunks)


def test_every_non_whitespace_character_of_the_source_survives_splitting() -> None:
    # This does not claim byte-identical content: the split path normalises
    # whitespace (see `_split_section`'s docstring), so this only checks the
    # weaker, honest guarantee — every non-whitespace character still shows
    # up, in order, somewhere across the chunks.
    body = f"# Returns Policy\n\n## Exceptions\n\n{LONG_SENTENCES}\n"
    chunks = chunk_markdown(_document(body), max_chars=500, overlap_chars=100)

    joined = _squeeze("".join(chunk.content for chunk in chunks))
    assert _is_subsequence(_squeeze(LONG_SENTENCES), joined)


def test_consecutive_chunks_of_one_section_overlap() -> None:
    body = f"# Returns Policy\n\n## Exceptions\n\n{LONG_SENTENCES}\n"
    chunks = chunk_markdown(_document(body), max_chars=500, overlap_chars=100)

    first, second = chunks[0], chunks[1]
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


def test_budget_too_small_relative_to_overlap_is_an_error() -> None:
    # heading_path is "Returns Policy > " (17 chars) + "H" * 250 (250 chars)
    # == 267 chars; budget is 320 - 267 - len("\n\n") == 51, which is
    # <= overlap_chars=100. This guard (chunk_markdown, before any splitting
    # is attempted) must fire here, not the sliding-window guard inside
    # _split_section — the two are checked at different points and must
    # raise distinguishable messages. Prose is 100 chars, comfortably above
    # the 51-char budget, so the guard's `len(prose) > budget` condition
    # holds.
    body = "# Returns Policy\n\n## " + "H" * 250 + "\n\n" + "word " * 20 + "\n"
    with pytest.raises(ChunkingError, match="leaves too little budget"):
        chunk_markdown(_document(body), max_chars=320, overlap_chars=100)


def test_overlap_leaving_no_room_to_advance_is_an_error() -> None:
    # heading_path is "Returns Policy > " (17 chars) + "H" * 200 (200 chars)
    # == 217 chars; budget is 320 - 217 - len("\n\n") == 101, which is above
    # overlap_chars=100, so the budget-too-small guard above does not fire.
    # But packed_budget inside _split_section is 101 - 100 - len("\n\n") ==
    # -1: the sliding window could never advance. This is a second, later
    # guard and must be exercised on its own — not incidentally satisfied by
    # the first guard's message. Prose is 125 chars, above the 101-char
    # budget, so the fast path in `_split_section` is skipped.
    body = "# Returns Policy\n\n## " + "H" * 200 + "\n\n" + "word " * 25 + "\n"
    with pytest.raises(ChunkingError, match="leaves no room to advance"):
        chunk_markdown(_document(body), max_chars=320, overlap_chars=100)


def test_hash_comment_inside_backtick_fence_is_not_a_heading() -> None:
    body = (
        "## Real section\n"
        "\n"
        "Before code.\n"
        "\n"
        "```bash\n"
        "# this is a shell comment\n"
        "echo ok\n"
        "```\n"
        "\n"
        "After code.\n"
    )
    chunks = chunk_markdown(_document(body), max_chars=2000, overlap_chars=500)

    assert len(chunks) == 1
    assert chunks[0].heading_path == "Returns Policy > Real section"
    assert "```bash" in chunks[0].content
    assert "# this is a shell comment" in chunks[0].content
    assert "echo ok" in chunks[0].content
    assert "After code." in chunks[0].content


def test_atx_heading_inside_tilde_fence_is_not_a_heading() -> None:
    body = (
        "## Real section\n"
        "\n"
        "Before code.\n"
        "\n"
        "~~~\n"
        "## Not a heading\n"
        "~~~\n"
        "\n"
        "After code.\n"
    )
    chunks = chunk_markdown(_document(body), max_chars=2000, overlap_chars=500)

    assert len(chunks) == 1
    assert "## Not a heading" in chunks[0].content


def test_fence_with_info_string_attributes() -> None:
    body = (
        "## Real section\n"
        "\n"
        'Before code.\n\n```python title="setup.py"\n# comment\n```\n\nAfter code.\n'
    )
    chunks = chunk_markdown(_document(body), max_chars=2000, overlap_chars=500)

    assert len(chunks) == 1


def test_indented_fence_up_to_three_spaces_opens_a_fence() -> None:
    body = "## Real section\n\nBefore code.\n\n   ```\n# comment\n   ```\n\nAfter code.\n"
    chunks = chunk_markdown(_document(body), max_chars=2000, overlap_chars=500)

    assert len(chunks) == 1


def test_longer_fence_closes_only_on_equal_or_longer_run() -> None:
    body = "## Real section\n\n````\n```\n# comment\n````\n\nAfter code.\n"
    chunks = chunk_markdown(_document(body), max_chars=2000, overlap_chars=500)

    assert len(chunks) == 1
    assert "```" in chunks[0].content


def test_unclosed_fence_swallows_the_rest_of_the_document() -> None:
    body = "## Real section\n\n```\n## Later heading\n"
    chunks = chunk_markdown(_document(body), max_chars=2000, overlap_chars=500)

    assert len(chunks) == 1
    assert "Later heading" not in chunks[0].heading_path
    # Without this line the test passes against the pre-fix chunker too: the
    # stray heading merely opened a prose-less section that the no-prose rule
    # dropped, so the heading_path assertion alone proved nothing. What the
    # fix actually changes is that the fence swallows the line as content.
    assert "## Later heading" in chunks[0].content


def test_headings_outside_fences_still_split_sections() -> None:
    body = "## A\n\nAlpha.\n\n```\n# noise\n```\n\n## B\n\nBravo.\n"
    chunks = chunk_markdown(_document(body), max_chars=2000, overlap_chars=500)

    assert [chunk.heading_path for chunk in chunks] == [
        "Returns Policy > A",
        "Returns Policy > B",
    ]
    a_chunk = next(c for c in chunks if c.heading_path.endswith("A"))
    assert "# noise" in a_chunk.content


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


def test_a_document_with_no_prose_is_an_error() -> None:
    # Only headings, no prose anywhere: the "no prose, no chunk" rule drops
    # every section, leaving nothing. A document left with nothing at all is
    # an authoring error, not a silent empty chunk list.
    body = "## Only a heading\n\n### Nested heading\n"
    with pytest.raises(ChunkingError, match="returns-policy"):
        chunk_markdown(_document(body), max_chars=2000, overlap_chars=500)


def test_a_document_whose_body_is_only_a_title_heading_is_an_error() -> None:
    # The title-H1 rule strips this heading as structure, not a section,
    # leaving no sections at all.
    body = "# Returns Policy\n"
    with pytest.raises(ChunkingError, match="returns-policy"):
        chunk_markdown(_document(body), max_chars=2000, overlap_chars=500)


def test_negative_overlap_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        chunk_markdown(_document("Just prose."), max_chars=2000, overlap_chars=-1)


def test_zero_max_chars_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        chunk_markdown(_document("Just prose."), max_chars=0, overlap_chars=0)


def test_overlap_at_or_above_half_of_max_chars_is_rejected() -> None:
    with pytest.raises(ValueError, match="less than half of max_chars"):
        chunk_markdown(_document("Just prose."), max_chars=100, overlap_chars=50)
    with pytest.raises(ValueError, match="less than half of max_chars"):
        chunk_markdown(_document("Just prose."), max_chars=100, overlap_chars=60)
