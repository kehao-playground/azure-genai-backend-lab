"""What Azure AI Search requires of us: key rules, vector width, index schema.

This module is the single source of truth for the index definition. The schema
is code so that it can be reviewed, diffed and drift-checked like the OpenAPI
document, rather than living as a JSON blob someone edited in the portal.
"""

import re
from typing import Any

# text-embedding-3-small emits between 1 and 1536 dimensions (Matryoshka
# truncation). We take the full width.
#
# This is a constant, not a setting. The index schema, the embeddings request
# and the response-length check must agree on one number, and a setting is
# precisely a mechanism for letting them disagree at runtime. Changing it means
# a different embedding space: drop the index, rebuild it, and re-embed every
# chunk.
EMBEDDING_DIMENSIONS = 1536

# Document key rules (checked 2026-07):
# letters, digits, '-', '_', '='; at most 1024 characters; the first character
# may not be an underscore. Anything else must be URL-safe Base64 encoded.
DOCUMENT_KEY_MAX_LENGTH = 1024
_DOCUMENT_KEY_PATTERN = re.compile(r"^[A-Za-z0-9\-=][A-Za-z0-9_\-=]*$")


class DocumentKeyError(ValueError):
    """A value cannot be used as an Azure AI Search document key."""


def validate_document_key(value: str, *, field: str) -> str:
    """Return ``value`` unchanged, or raise if it is not a legal document key.

    ``field`` names the offending value in the message: the same rule guards
    an authored ``doc_id`` and a derived ``chunk_id``, and the two failures
    need different fixes.
    """
    if not value:
        raise DocumentKeyError(f"{field} must not be empty")
    if len(value) > DOCUMENT_KEY_MAX_LENGTH:
        raise DocumentKeyError(
            f"{field} is {len(value)} characters; the document key limit is "
            f"{DOCUMENT_KEY_MAX_LENGTH}"
        )
    if not _DOCUMENT_KEY_PATTERN.match(value):
        raise DocumentKeyError(
            f"{field} {value!r} is not a valid document key: use letters, digits, "
            "'-', '_' or '=', and do not begin with '_'"
        )
    return value


INDEX_NAME = "azgenai-lab-chunks"

_HNSW_ALGORITHM = "chunk-hnsw"
_VECTOR_PROFILE = "chunk-vector-profile"


def _text_field(
    name: str,
    *,
    searchable: bool = False,
    filterable: bool = False,
    sortable: bool = False,
    facetable: bool = False,
    key: bool = False,
    analyzer: str | None = None,
) -> dict[str, Any]:
    field: dict[str, Any] = {
        "name": name,
        "type": "Edm.String",
        "retrievable": True,
        "searchable": searchable,
        "filterable": filterable,
        "sortable": sortable,
        "facetable": facetable,
    }
    if key:
        field["key"] = True
    if analyzer is not None:
        field["analyzer"] = analyzer
    return field


def to_index_definition() -> dict[str, Any]:
    """The index schema, as the Create-or-Update Index REST body expects it.

    One chunk is one search document. ``parent_id`` is what makes a document's
    chunks addressable as a set, which is what the replacement strategy needs
    (upload the new chunks, then delete the stale ones).

    Nothing here is filterable by accident: vector fields cannot be filtered,
    so every query-time restriction — including Day 15's tenant isolation —
    has to ride on a scalar field that exists from the first build.
    """
    return {
        "name": INDEX_NAME,
        "fields": [
            _text_field("chunk_id", key=True, filterable=True),
            _text_field("parent_id", filterable=True),
            _text_field("title", searchable=True, filterable=True),
            _text_field("heading_path", searchable=True),
            _text_field("content", searchable=True, analyzer="en.microsoft"),
            _text_field("doc_type", filterable=True, facetable=True),
            _text_field("tenant_id", filterable=True),
            {
                "name": "effective_date",
                "type": "Edm.DateTimeOffset",
                "retrievable": True,
                "searchable": False,
                "filterable": True,
                "sortable": True,
                "facetable": False,
            },
            {
                "name": "content_vector",
                "type": "Collection(Edm.Single)",
                # Vector fields must be searchable and must not be filterable,
                # sortable or facetable.
                "searchable": True,
                "filterable": False,
                "sortable": False,
                "facetable": False,
                # stored=True (the service default) keeps the source copy of the
                # vector. That copy is what makes merge/mergeOrUpload safe, and
                # it is the only thing that lets a schema rebuild reload vectors
                # instead of paying the embedding bill twice. stored=False saves
                # up to half the field's disk, cannot be undone, and drops
                # vectors on a partial update without an error or a warning.
                "stored": True,
                # Raw vectors are not useful to a client; retrievable can be
                # flipped later precisely because stored is True.
                "retrievable": False,
                "dimensions": EMBEDDING_DIMENSIONS,
                "vectorSearchProfile": _VECTOR_PROFILE,
            },
        ],
        "vectorSearch": {
            "algorithms": [
                {
                    "name": _HNSW_ALGORITHM,
                    "kind": "hnsw",
                    # cosine is the similarity metric for Azure OpenAI embeddings.
                    "hnswParameters": {
                        "m": 4,
                        "efConstruction": 400,
                        "efSearch": 500,
                        "metric": "cosine",
                    },
                }
            ],
            "profiles": [{"name": _VECTOR_PROFILE, "algorithm": _HNSW_ALGORITHM}],
        },
    }
