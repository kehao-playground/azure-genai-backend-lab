"""Azure AI Search imposes rules on us; this module is where they live.

Document keys are validated here rather than at upload time because a bad key
is an authoring mistake, and Day 13 is the wrong place to discover it.
"""

import pytest

from azgenai_lab.models.search_index import (
    DOCUMENT_KEY_MAX_LENGTH,
    EMBEDDING_DIMENSIONS,
    DocumentKeyError,
    validate_document_key,
)


def test_embedding_dimensions_is_the_full_text_embedding_3_small_width() -> None:
    assert EMBEDDING_DIMENSIONS == 1536


@pytest.mark.parametrize(
    "value",
    ["returns-policy", "returns_policy", "Doc123", "a", "key=", "returns-policy-0000"],
)
def test_accepts_legal_keys(value: str) -> None:
    assert validate_document_key(value, field="chunk_id") == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "_leading-underscore",
        "has space",
        "has/slash",
        "has.dot",
        "unicode-中文",
    ],
)
def test_rejects_illegal_keys(value: str) -> None:
    with pytest.raises(DocumentKeyError):
        validate_document_key(value, field="chunk_id")


def test_rejects_keys_over_the_length_limit() -> None:
    assert validate_document_key("a" * DOCUMENT_KEY_MAX_LENGTH, field="chunk_id")
    with pytest.raises(DocumentKeyError):
        validate_document_key("a" * (DOCUMENT_KEY_MAX_LENGTH + 1), field="chunk_id")


def test_error_names_the_offending_field() -> None:
    with pytest.raises(DocumentKeyError, match="chunk_id"):
        validate_document_key("_bad", field="chunk_id")
