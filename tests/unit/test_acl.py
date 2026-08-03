import pytest

from azgenai_lab.models.principal import Principal
from azgenai_lab.services.acl import build_acl_filter, is_document_visible, require_acl_metadata


def test_filter_with_groups() -> None:
    p = Principal(tenant_id="tenant-a", user_id="u1", group_ids=("support", "finance"))
    assert build_acl_filter(p) == (
        "tenant_id eq 'tenant-a' and (not allowed_groups/any() "
        "or allowed_groups/any(g: search.in(g, 'finance,support')))"
    )


def test_filter_empty_groups_simplifies() -> None:
    p = Principal(tenant_id="tenant-a", user_id="u1", group_ids=())
    assert build_acl_filter(p) == "tenant_id eq 'tenant-a' and not allowed_groups/any()"


def test_filter_escapes_even_if_validation_bypassed() -> None:
    p = Principal.model_construct(tenant_id="a' or 1 eq 1", user_id="u1", group_ids=())
    assert build_acl_filter(p) == ("tenant_id eq 'a'' or 1 eq 1' and not allowed_groups/any()")


def test_search_in_delimiter_safety_depends_on_charset() -> None:
    # escape_odata_literal handles only the string-literal layer (quote doubling).
    # The search.in value list is comma-delimited, so a comma inside a group id
    # would split one id into two — that layer is protected solely by the
    # Principal identifier charset. This test pins both halves of the contract.
    with pytest.raises(ValueError):
        Principal(tenant_id="tenant-a", user_id="u1", group_ids=("finance,support",))
    with pytest.raises(ValueError):
        Principal(tenant_id="tenant-a", user_id="u1", group_ids=("finance support",))
    # If validation were bypassed, the filter WOULD be corrupted: the joined
    # list is indistinguishable from two separate ids. Loosening the charset
    # without moving to an explicit delimiter must revisit this.
    hostile = Principal.model_construct(
        tenant_id="tenant-a", user_id="u1", group_ids=("finance,support",)
    )
    assert "search.in(g, 'finance,support')" in build_acl_filter(hostile), (
        "delimiter collision is real when charset validation is bypassed"
    )


def test_visibility_wrong_tenant_false_even_with_group() -> None:
    doc = {"tenant_id": "tenant-b", "allowed_groups": ["finance"]}
    assert not is_document_visible(
        doc, Principal(tenant_id="tenant-a", user_id="u1", group_ids=("finance",))
    )


def test_visibility_empty_acl_is_tenant_wide() -> None:
    doc = {"tenant_id": "tenant-a", "allowed_groups": []}
    assert is_document_visible(doc, Principal(tenant_id="tenant-a", user_id="u1", group_ids=()))


def test_visibility_requires_intersection() -> None:
    doc = {"tenant_id": "tenant-a", "allowed_groups": ["finance"]}
    assert is_document_visible(
        doc, Principal(tenant_id="tenant-a", user_id="u1", group_ids=("finance", "x"))
    )
    assert not is_document_visible(
        doc, Principal(tenant_id="tenant-a", user_id="u1", group_ids=("support",))
    )


@pytest.mark.parametrize(
    "doc",
    [
        {},
        {"tenant_id": "t"},
        {"allowed_groups": []},
        {"tenant_id": "t", "allowed_groups": "finance"},
        {"tenant_id": 3, "allowed_groups": []},
    ],
)
def test_missing_or_mistyped_acl_metadata_raises(doc: dict) -> None:
    with pytest.raises(ValueError):
        require_acl_metadata(doc)
