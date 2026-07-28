"""Split Markdown documents into chunks (indexing stages: chunk and enrich).

Structure first: a chunk is a Markdown section, so a citation can name a
section a reader recognises. Only a section that will not fit is broken up,
and then by the largest natural boundary available.

The supported Markdown subset is deliberately narrow: ATX headings
(``#`` through ``######``) outside fenced code blocks, blank-line paragraph
boundaries, and fenced code blocks (``` ``` ``` or ``~~~``) treated as opaque
spans that never contribute headings. Setext headings (underlined with ``=``
or ``-``), 4-space-indented code blocks, and HTML blocks are **not**
recognised — an indented code block containing a ``#`` line is still read as
a heading, because indentation alone carries no meaning to this splitter.

Everything here is a pure function of its arguments. No I/O, no settings
lookups, no clock — the same document and parameters always produce the same
chunks, which is what makes chunk ids stable.
"""

import re

from azgenai_lab.models.rag import (
    EMBEDDING_JOIN,
    HEADING_SEPARATOR,
    Chunk,
    SourceDocument,
    make_chunk_id,
)

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")

# CommonMark fenced code blocks, to the depth described in the module
# docstring: a fence opens on a line whose first non-whitespace run is three
# or more backticks or tildes, indented by up to three spaces. `group(1)` is
# the run itself (its first character is the fence character, its length is
# the minimum a closing run must meet); `group(2)` is the info string.
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _fence_open(line: str) -> tuple[str, int] | None:
    """Return ``(fence_char, run_length)`` if ``line`` opens a fence, else ``None``.

    A backtick fence's info string must not itself contain a backtick — that
    ambiguity is CommonMark's rule, not just decoration — so such a line does
    not open a fence at all and is left for the caller to treat as ordinary
    content. A tilde fence's info string has no such restriction.
    """
    match = _FENCE_OPEN.match(line)
    if match is None:
        return None
    run, info_string = match.group(1), match.group(2)
    fence_char = run[0]
    if fence_char == "`" and "`" in info_string:
        return None
    return fence_char, len(run)


def _fence_closes(line: str, fence_char: str, min_length: int) -> bool:
    """Whether ``line`` closes a fence opened with ``fence_char`` and ``min_length``.

    The closing run must be the same character, at least as long as the
    opening run, indented by up to three spaces, with nothing but trailing
    whitespace after it — a shorter or different-character run is content,
    not a close.
    """
    pattern = rf"^ {{0,3}}{re.escape(fence_char)}{{{min_length},}}[ \t]*$"
    return re.match(pattern, line) is not None

# Sentence terminators for both writing systems, plus any trailing closing
# punctuation. Chinese has no inter-word spaces, so a whitespace-based splitter
# would treat a whole paragraph as one token and never split it.
#
# The trailing `\s*` is allowed to match zero characters, which is what lets
# this fire between two CJK characters with no space between them. The same
# leniency means "99.9%" or "e.g." reads as a sentence boundary too — a false
# positive this splitter accepts, since the worst it does is move a boundary
# to a slightly different point inside a paragraph that was already too big
# to be one chunk.
_SENTENCE_END = re.compile(r"[.!?。！？]['\"”’」』)）]*\s*")
_PARAGRAPH_JOIN = "\n\n"


class ChunkingError(Exception):
    """A document cannot be chunked under the given parameters."""


def _budget_error(
    doc_id: str, heading_path: str, budget: int, max_chars: int, overlap_chars: int
) -> ChunkingError:
    return ChunkingError(
        f"{doc_id}: heading path {heading_path!r} leaves too little "
        f"budget ({budget} chars) for content within max_chars={max_chars} and "
        f"overlap_chars={overlap_chars}"
    )


def _sections(document: SourceDocument) -> list[tuple[str, str]]:
    """Return ``(heading_path, prose)`` pairs in document order.

    The document title is always the first element of a heading path, whether
    or not the body repeats it as an H1: the path is what identifies the chunk
    inside the embedding input, so it can never be anonymous.

    A heading with no prose of its own is dropped, uniformly — including a
    grouping heading such as "Refund window" that has only deeper headings
    ("Standard purchases", "Promotional purchases") nested under it and no
    prose of its own. This is a deliberate product decision, not an oversight:
    no prose, no chunk. The heading is not lost, though — it survives as a
    breadcrumb segment inside every descendant's ``heading_path``, so it is
    still searchable and citable through its children. The implicit preamble
    section (before the first heading, path equal to the bare title) follows
    the same rule: kept only when it actually has something to say.
    """
    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    # None when scanning ordinary lines; otherwise the fence currently open,
    # so an ATX-looking line inside it is content, never a heading.
    fence: tuple[str, int] | None = None

    def flush() -> None:
        prose = "\n".join(buffer).strip()
        buffer.clear()
        if not prose:
            return
        if stack:
            path = HEADING_SEPARATOR.join([document.title, *(text for _, text in stack)])
        else:
            path = document.title
        sections.append((path, prose))

    for line in document.body.splitlines():
        if fence is not None:
            buffer.append(line)
            if _fence_closes(line, fence[0], fence[1]):
                fence = None
            continue
        opened = _fence_open(line)
        if opened is not None:
            fence = opened
            buffer.append(line)
            continue
        match = _HEADING.match(line)
        if match is None:
            buffer.append(line)
            continue
        level, text = len(match.group(1)), match.group(2)
        flush()
        # An H1 that repeats the document title is structure, not a section.
        if level == 1 and text == document.title:
            stack = []
            continue
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, text))
    flush()
    return sections


def chunk_markdown(
    document: SourceDocument, *, max_chars: int, overlap_chars: int
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for heading_path, prose in _sections(document):
        budget = max_chars - len(heading_path) - len(EMBEDDING_JOIN)
        if budget <= 0:
            # The heading path alone does not fit max_chars; no prose could
            # ever fit either.
            raise _budget_error(document.doc_id, heading_path, budget, max_chars, overlap_chars)
        if len(prose) > budget and budget <= overlap_chars:
            # This section must be split (its prose overflows budget), but a
            # sliding window could never advance: each piece would be no
            # bigger than the overlap it repeats from the last one. Fail fast
            # here rather than let that surface as an infinite loop in
            # `_split_section`.
            raise _budget_error(document.doc_id, heading_path, budget, max_chars, overlap_chars)
        for piece in _split_section(prose, budget=budget, overlap=overlap_chars):
            chunks.append(
                Chunk(
                    chunk_id=make_chunk_id(document.doc_id, len(chunks)),
                    parent_id=document.doc_id,
                    title=document.title,
                    heading_path=heading_path,
                    content=piece,
                    doc_type=document.doc_type,
                    tenant_id=document.tenant_id,
                    effective_date=document.effective_date,
                )
            )
    return chunks


def _split_section(prose: str, *, budget: int, overlap: int) -> list[str]:
    if len(prose) <= budget:
        # Fast path: the section already fits, so it is returned untouched.
        # The paragraph-stripping and "\n\n" re-joining below only happen to
        # sections that get split — a chunk's whitespace is either the
        # author's, verbatim, or a side effect of splitting, never both for
        # the same chunk. That asymmetry is accepted rather than normalising
        # every section, so an unsplit chunk is exactly what the author wrote.
        return [prose]

    # Pieces are packed to leave room for the overlap tail that will be
    # prepended to every piece after the first, so the finished chunk still
    # fits the budget. This one budget is applied uniformly to every piece,
    # including pieces[0], even though the first piece never receives an
    # overlap tail and so is under-packed by `overlap + len(_PARAGRAPH_JOIN)`
    # characters relative to what it could actually hold. That waste is
    # accepted deliberately: a single budget number governing every piece is
    # worth more than reclaiming the last fraction of the first chunk.
    packed_budget = budget - overlap - len(_PARAGRAPH_JOIN) if overlap else budget
    if packed_budget <= 0:
        raise ChunkingError(
            f"overlap of {overlap} characters leaves no room to advance within a "
            f"content budget of {budget} characters"
        )

    units: list[str] = []
    for paragraph in prose.split(_PARAGRAPH_JOIN):
        stripped = paragraph.strip()
        if stripped:
            units.extend(_split_unit(stripped, packed_budget))

    pieces: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}{_PARAGRAPH_JOIN}{unit}" if current else unit
        if len(candidate) <= packed_budget:
            current = candidate
            continue
        if current:
            pieces.append(current)
        current = unit
    if current:
        pieces.append(current)

    return _apply_overlap(pieces, overlap=overlap)


def _split_unit(unit: str, limit: int) -> list[str]:
    """Break one paragraph down until every part fits, sentences first."""
    if len(unit) <= limit:
        return [unit]

    parts: list[str] = []
    current = ""
    for sentence in _sentences(unit):
        candidate = current + sentence
        if len(candidate) <= limit:
            current = candidate
            continue
        if current.strip():
            parts.append(current.strip())
        current = ""
        if len(sentence) <= limit:
            current = sentence
            continue
        # No sentence boundary is close enough: cut at the limit. This is the
        # only place a chunk boundary carries no meaning, and it is recorded
        # here rather than pretended away.
        for start in range(0, len(sentence), limit):
            parts.append(sentence[start : start + limit].strip())
    if current.strip():
        parts.append(current.strip())
    return [part for part in parts if part]


def _sentences(text: str) -> list[str]:
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        sentences.append(text[start : match.end()])
        start = match.end()
    if start < len(text):
        sentences.append(text[start:])
    return sentences


def _apply_overlap(pieces: list[str], *, overlap: int) -> list[str]:
    if overlap <= 0 or len(pieces) < 2:
        return pieces
    result = [pieces[0]]
    # pieces[:-1] and pieces[1:] are the same length; zipping pieces against
    # pieces[1:] under strict=True raises after the last pair.
    for previous, piece in zip(pieces[:-1], pieces[1:], strict=True):
        tail = _sentence_aligned_tail(previous, overlap)
        result.append(f"{tail}{_PARAGRAPH_JOIN}{piece}" if tail else piece)
    return result


def _sentence_aligned_tail(text: str, limit: int) -> str:
    """The longest suffix of ``text`` that fits ``limit`` and starts a sentence.

    Azure AI Search's built-in Text Split skill takes a fixed number of
    trailing characters for its overlap, which routinely opens the next chunk
    mid-sentence. Since this splitter is structural everywhere else, its
    overlap is structural too.
    """
    if len(text) <= limit:
        return text
    window_start = len(text) - limit
    for match in _SENTENCE_END.finditer(text, window_start):
        if match.end() < len(text):
            return text[match.end() :]
        break
    return text[window_start:]
