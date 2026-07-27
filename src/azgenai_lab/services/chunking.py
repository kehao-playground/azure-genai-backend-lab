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


def _sections(document: SourceDocument) -> list[tuple[str, str]]:
    """Return ``(heading_path, prose)`` pairs in document order.

    The document title is always the first element of a heading path, whether
    or not the body repeats it as an H1: the path is what identifies the chunk
    inside the embedding input, so it can never be anonymous.

    A heading with no prose of its own is dropped, *unless* it is immediately
    followed by a deeper heading. That heading is a grouping node — "Refund
    window" with "Standard purchases" and "Promotional purchases" nested
    under it — and a reader can still cite it by name, so it is kept even
    though its own content is empty. The implicit preamble section (before
    the first heading, path equal to the bare title) never gets this
    exception: it is not a heading a reader can point to, so it is kept only
    when it actually has something to say.
    """
    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []

    def flush(next_level: int | None) -> None:
        prose = "\n".join(buffer).strip()
        buffer.clear()
        if not stack:
            if not prose:
                return
            path = document.title
        else:
            has_children = next_level is not None and next_level > stack[-1][0]
            if not prose and not has_children:
                return
            path = _HEADING_SEPARATOR.join([document.title, *(text for _, text in stack)])
        sections.append((path, prose))

    for line in document.body.splitlines():
        match = _HEADING.match(line)
        if match is None:
            buffer.append(line)
            continue
        level, text = len(match.group(1)), match.group(2)
        flush(level)
        # An H1 that repeats the document title is structure, not a section.
        if level == 1 and text == document.title:
            stack = []
            continue
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, text))
    flush(None)
    return sections


def chunk_markdown(
    document: SourceDocument, *, max_chars: int, overlap_chars: int
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for heading_path, prose in _sections(document):
        budget = max_chars - len(heading_path) - len(_EMBEDDING_JOIN)
        # A section that Task 7 ever needs to split must leave more than one
        # overlap's worth of room, or a sliding window could never advance:
        # each piece would be no bigger than the overlap it repeats from the
        # last one. Fail fast here rather than let that surface as a Task 7
        # infinite-loop bug.
        if budget <= overlap_chars:
            raise ChunkingError(
                f"{document.doc_id}: heading path {heading_path!r} leaves too little "
                f"budget ({budget} chars) for content within max_chars={max_chars} and "
                f"overlap_chars={overlap_chars}"
            )
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
