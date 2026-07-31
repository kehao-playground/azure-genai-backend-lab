"""Azure AI Search imposes rules on us; this module is where they live.

Document keys are validated here rather than at upload time because a bad key
is an authoring mistake, and Day 13 is the wrong place to discover it.
"""

import dataclasses
from typing import Any

import pytest

from azgenai_lab.models.rag import Chunk
from azgenai_lab.models.search import SearchMode
from azgenai_lab.models.search_index import (
    DOCUMENT_KEY_MAX_LENGTH,
    EMBEDDING_DIMENSIONS,
    INDEX_NAME,
    SEARCH_API_VERSION,
    SEMANTIC_CONFIGURATION_NAME,
    VECTOR_FIELD,
    DocumentKeyError,
    to_index_definition,
    validate_document_key,
)
from azgenai_lab.services.azure_search import build_search_body


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


def _field(name: str) -> dict[str, Any]:
    fields = to_index_definition()["fields"]
    return next(field for field in fields if field["name"] == name)


def test_index_has_exactly_one_key_field() -> None:
    keys = [field["name"] for field in to_index_definition()["fields"] if field.get("key")]
    assert keys == ["chunk_id"]


def test_index_name_is_stable() -> None:
    assert INDEX_NAME == "azgenai-lab-chunks"


def test_all_expected_fields_are_present() -> None:
    # The eight scalar fields are pinned against `Chunk`'s own dataclass
    # fields, not a second hand-maintained literal, so a name added to one
    # side and not the other fails here instead of drifting silently.
    # `content_vector` has no `Chunk` field (it is derived at embed time), so
    # it is handled explicitly rather than folded into the comparison.
    chunk_field_names = {field.name for field in dataclasses.fields(Chunk)}
    names = {field["name"] for field in to_index_definition()["fields"]}
    assert names == chunk_field_names | {"content_vector"}


def test_vector_field_keeps_its_source_copy() -> None:
    # stored=False is irreversible and silently drops vectors on a partial
    # update. If this assertion ever fails, read the docstring before changing
    # it: the storage saving is not worth the failure mode.
    vector = _field("content_vector")
    assert vector["stored"] is True
    assert vector["retrievable"] is False


def test_vector_field_obeys_the_service_constraints() -> None:
    vector = _field("content_vector")
    assert vector["type"] == "Collection(Edm.Single)"
    assert vector["searchable"] is True
    assert vector["filterable"] is False
    assert vector["sortable"] is False
    assert vector["facetable"] is False
    assert vector["dimensions"] == EMBEDDING_DIMENSIONS


def test_tenant_id_is_filterable_for_day_15() -> None:
    assert _field("tenant_id")["filterable"] is True


def test_effective_date_is_filterable_and_sortable() -> None:
    effective_date = _field("effective_date")
    assert effective_date["type"] == "Edm.DateTimeOffset"
    assert effective_date["filterable"] is True
    assert effective_date["sortable"] is True


def test_vector_profile_referenced_by_the_vector_field_exists() -> None:
    definition = to_index_definition()
    profile_names = {profile["name"] for profile in definition["vectorSearch"]["profiles"]}
    assert _field("content_vector")["vectorSearchProfile"] in profile_names


def test_vector_algorithm_uses_cosine_for_azure_openai_embeddings() -> None:
    algorithms = to_index_definition()["vectorSearch"]["algorithms"]
    assert algorithms[0]["kind"] == "hnsw"
    assert algorithms[0]["hnswParameters"]["metric"] == "cosine"


def test_chunk_id_is_sortable_because_stale_enumeration_pages_by_it() -> None:
    # Cursor paging needs a unique field that is both filterable and sortable,
    # and sortable cannot be enabled on an existing field — turning it on
    # later costs a full rebuild.
    chunk_id = _field("chunk_id")
    assert chunk_id["key"] is True
    assert chunk_id["filterable"] is True
    assert chunk_id["sortable"] is True


def test_semantic_configuration_is_declared_and_default() -> None:
    semantic = to_index_definition()["semantic"]
    assert semantic["defaultConfiguration"] == SEMANTIC_CONFIGURATION_NAME
    configuration = semantic["configurations"][0]
    assert configuration["name"] == SEMANTIC_CONFIGURATION_NAME
    prioritized = configuration["prioritizedFields"]
    assert prioritized["titleField"] == {"fieldName": "title"}
    assert prioritized["prioritizedContentFields"] == [{"fieldName": "content"}]
    assert prioritized["prioritizedKeywordsFields"] == [{"fieldName": "heading_path"}]


def test_semantic_fields_are_searchable_and_retrievable() -> None:
    # The service requires every field named in a semantic configuration to be
    # both searchable and retrievable; naming an ineligible field is rejected
    # at index-creation time.
    # The names are read out of the configuration rather than repeated here, so
    # that pointing the configuration at an ineligible field fails this test
    # instead of waiting for the service to reject the index.
    definition = to_index_definition()
    prioritized = definition["semantic"]["configurations"][0]["prioritizedFields"]

    field_names = set()
    if "titleField" in prioritized:
        field_names.add(prioritized["titleField"]["fieldName"])
    for group in ("prioritizedContentFields", "prioritizedKeywordsFields"):
        for field in prioritized.get(group, []):
            field_names.add(field["fieldName"])

    # Without this, a configuration that named no fields would pass vacuously.
    assert field_names, "the semantic configuration names no fields"

    for name in field_names:
        field = _field(name)
        assert field["searchable"] is True, name
        assert field["retrievable"] is True, name


def test_the_vector_field_has_one_name_across_the_schema_and_the_query() -> None:
    # The field is located by its *type*, not by VECTOR_FIELD: looking it up by
    # the constant would make the comparison agree with itself. Two artifacts
    # have to name the same field — the index that stores the vectors and the
    # query that searches them — and a mismatch is not a build error, a type
    # error or a 4xx. It is a live query that quietly returns nothing.
    vector_fields = [
        field
        for field in to_index_definition()["fields"]
        if field["type"] == "Collection(Edm.Single)"
    ]
    assert len(vector_fields) == 1
    assert vector_fields[0]["name"] == VECTOR_FIELD

    body = build_search_body(
        "q",
        [0.1] * EMBEDDING_DIMENSIONS,
        mode=SearchMode.VECTOR,
        top=5,
        filter="tenant_id eq 't1'",
    )
    assert body["vectorQueries"][0]["fields"] == VECTOR_FIELD


def test_allowed_groups_acl_field_shape() -> None:
    """Verify allowed_groups field has exactly the seven required keys and no stored key.

    allowed_groups is a Collection(Edm.String) used for ACL filtering via the
    'allowed_groups/any(...)' syntax. The stored key is not set because it is
    only valid on vector fields; setting it on a string collection is rejected
    by the Azure AI Search service.
    """
    allowed_groups = _field("allowed_groups")
    # Verify all seven required keys are present.
    assert allowed_groups["name"] == "allowed_groups"
    assert allowed_groups["type"] == "Collection(Edm.String)"
    assert allowed_groups["searchable"] is False
    assert allowed_groups["filterable"] is True
    assert allowed_groups["retrievable"] is False
    assert allowed_groups["sortable"] is False
    assert allowed_groups["facetable"] is False
    # Explicitly verify that stored key is NOT present.
    assert "stored" not in allowed_groups


def test_constants_are_pinned() -> None:
    assert SEARCH_API_VERSION == "2026-04-01"
    assert VECTOR_FIELD == "content_vector"
