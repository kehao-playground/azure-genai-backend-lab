"""Split Markdown documents into chunks (indexing stages: chunk and enrich).

Structure first: a chunk is a Markdown section, so a citation can name a
section a reader recognises. Only a section that will not fit is broken up,
and then by the largest natural boundary available (Task 7).

Everything here is a pure function of its arguments. No I/O, no settings
lookups, no clock — the same document and parameters always produce the same
chunks, which is what makes chunk ids stable.
"""

import re

from azgenai_lab.models.rag import Chunk, SourceDocument, make_chunk_id

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
_HEADING_SEPARATOR = " > "
_EMBEDDING_JOIN = "\n\n"


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

    def flush() -> None:
        prose = "\n".join(buffer).strip()
        buffer.clear()
        if not prose:
            return
        if stack:
            path = _HEADING_SEPARATOR.join([document.title, *(text for _, text in stack)])
        else:
            path = document.title
        sections.append((path, prose))

    for line in document.body.splitlines():
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
        budget = max_chars - len(heading_path) - len(_EMBEDDING_JOIN)
        if budget <= 0:
            # The heading path alone does not fit max_chars; no prose could
            # ever fit either.
            raise _budget_error(document.doc_id, heading_path, budget, max_chars, overlap_chars)
        if len(prose) > budget and budget <= overlap_chars:
            # This section must be split (its prose overflows budget), but a
            # sliding window could never advance: each piece would be no
            # bigger than the overlap it repeats from the last one. Fail fast
            # here rather than let that surface as a Task 7 infinite-loop bug.
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
    """Task 7 replaces this with real splitting."""
    return [prose]
