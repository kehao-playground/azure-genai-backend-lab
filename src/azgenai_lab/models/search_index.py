"""What Azure AI Search requires of us: key rules, vector width, index schema.

This module is the single source of truth for the index definition. The schema
is code so that it can be reviewed, diffed and drift-checked like the OpenAPI
document, rather than living as a JSON blob someone edited in the portal.
"""

import re

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
