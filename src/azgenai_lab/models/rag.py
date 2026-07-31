"""RAG data transfer objects shared across the indexing and query pipelines."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from azgenai_lab.models.search_index import (
    EMBEDDING_DIMENSIONS,
    VECTOR_FIELD,
    validate_document_key,
)

# A doc_id is authored by a human in front matter, and the derived chunk key
# must fit inside the service's 1024-character limit. Rather than compute the
# residual budget, hold authored ids to a human scale: an id longer than this
# is a naming mistake, not a capacity problem.
DOC_ID_MAX_LENGTH = 64

# Public, not module-private: `services/chunking.py` imports both to build
# `heading_path` and to size its budget against the same join `Chunk` uses to
# build `embedding_input`. This module owns them because it owns
# `embedding_input`; a leading underscore would misstate that they are meant
# to cross that boundary.
HEADING_SEPARATOR = " > "
EMBEDDING_JOIN = "\n\n"


class IndexingAction(StrEnum):
    """What an indexing request does to the documents it carries.

    Two actions, named for their effect rather than for a service's spelling
    of it. The write path is a replacement: chunks are written whole (there is
    no partial update of a chunk) and stale ones are removed. Which JSON field
    carries this, and what string goes in it, belongs to the adapter that
    builds the request.
    """

    UPSERT = "upsert"
    REMOVE = "remove"


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
    allowed_groups: tuple[str, ...]
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
    allowed_groups: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_document_key(self.chunk_id, field="chunk_id")
        validate_document_key(self.parent_id, field="parent_id")
        # The title must be a whole leading *segment* of the path, not merely a
        # character prefix: title "Return" against path "Returns Policy > ..."
        # is a mismatch, and a substring check would wave it through.
        if self.heading_path != self.title and not self.heading_path.startswith(
            self.title + HEADING_SEPARATOR
        ):
            raise ValueError(
                f"heading_path {self.heading_path!r} must begin with the document title "
                f"{self.title!r} as a complete segment: the embedding input is the only "
                "place the source document is named"
            )

    @property
    def embedding_input(self) -> str:
        return f"{self.heading_path}{EMBEDDING_JOIN}{self.content}"

    def to_index_document(self, vector: Sequence[float]) -> dict[str, Any]:
        """Render this chunk as one Azure AI Search document.

        ``effective_date`` is the one field whose contract does not close
        inside the dataclass. The source is a bare ``date``; the index field
        is ``Edm.DateTimeOffset``, which requires an offset. This project
        therefore *defines* the field as a UTC calendar date and encodes it at
        UTC midnight — the offset is a domain decision, not something the
        schema derives for us. Every range filter over this field must use UTC
        date boundaries or it will silently shift by a day.
        """
        if len(vector) != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"vector for chunk {self.chunk_id} has {len(vector)} dimensions; "
                f"the index expects {EMBEDDING_DIMENSIONS}"
            )
        return {
            "chunk_id": self.chunk_id,
            "parent_id": self.parent_id,
            "title": self.title,
            "heading_path": self.heading_path,
            "content": self.content,
            "doc_type": self.doc_type,
            "tenant_id": self.tenant_id,
            "effective_date": f"{self.effective_date.isoformat()}T00:00:00Z",
            "allowed_groups": list(self.allowed_groups),
            VECTOR_FIELD: list(vector),
        }


def make_parent_id(tenant_id: str, doc_id: str) -> str:
    """Derive a parent key from tenant and document identifiers.

    The format encodes both lengths to prevent collision: two pairs of
    (tenant, doc) cannot produce the same key even if their concatenations
    would (e.g., "a--b" + "c" vs "a" + "b--c"). The length prefix acts as
    a delimiter within the equality-constrained key alphabet.
    """
    return f"t{len(tenant_id)}={tenant_id}d{len(doc_id)}={doc_id}"


def make_chunk_id(parent_id: str, ordinal: int) -> str:
    """Derive a chunk key from its parent and position.

    Position-based ids shift when a document is edited, so a document's chunks
    must be replaced as a set, never updated one by one.
    """
    return f"{parent_id}-{ordinal:04d}"
