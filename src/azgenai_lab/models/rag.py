"""RAG data transfer objects shared across the indexing and query pipelines."""

from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel

from azgenai_lab.models.search_index import validate_document_key

# A doc_id is authored by a human in front matter, and the derived chunk key
# must fit inside the service's 1024-character limit. Rather than compute the
# residual budget, hold authored ids to a human scale: an id longer than this
# is a naming mistake, not a capacity problem.
DOC_ID_MAX_LENGTH = 64

_HEADING_SEPARATOR = " > "
_EMBEDDING_JOIN = "\n\n"


class Citation(BaseModel):
    source: str
    title: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class SourceDocument:
    """One authored file, before chunking. ``body`` excludes the front matter."""

    doc_id: str
    title: str
    doc_type: str
    tenant_id: str
    effective_date: date
    body: str


@dataclass(frozen=True)
class Chunk:
    """One chunk is one search document.

    Two fields carry text and they are not interchangeable. ``content`` is the
    source text a citation points at — what a reader is shown. What gets
    embedded is :attr:`embedding_input`, which prefixes the heading path so a
    chunk from the middle of a document still says which document and which
    section it came from. Embedding text and display text are deliberately
    different; treating them as one loses either context or citation fidelity.
    """

    chunk_id: str
    parent_id: str
    title: str
    heading_path: str
    content: str
    doc_type: str
    tenant_id: str
    effective_date: date

    def __post_init__(self) -> None:
        validate_document_key(self.chunk_id, field="chunk_id")
        validate_document_key(self.parent_id, field="parent_id")
        # The title must be a whole leading *segment* of the path, not merely a
        # character prefix: title "Return" against path "Returns Policy > ..."
        # is a mismatch, and a substring check would wave it through.
        if self.heading_path != self.title and not self.heading_path.startswith(
            self.title + _HEADING_SEPARATOR
        ):
            raise ValueError(
                f"heading_path {self.heading_path!r} must begin with the document title "
                f"{self.title!r} as a complete segment: the embedding input is the only "
                "place the source document is named"
            )

    @property
    def embedding_input(self) -> str:
        return f"{self.heading_path}{_EMBEDDING_JOIN}{self.content}"


def make_chunk_id(parent_id: str, ordinal: int) -> str:
    """Derive a chunk key from its parent and position.

    Position-based ids shift when a document is edited, so a document's chunks
    must be replaced as a set, never updated one by one.
    """
    return f"{parent_id}-{ordinal:04d}"
