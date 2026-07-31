import pytest
from pydantic import ValidationError

from azgenai_lab.models.principal import Principal, validate_identifier


@pytest.mark.parametrize("value", ["t1", "a-b_C9", "x" * 64])
def test_validate_identifier_accepts(value: str) -> None:
    assert validate_identifier(value, field="tenant_id") == value


@pytest.mark.parametrize("value", ["", "x" * 65, "a b", "a'b", "a,b", "日本"])
def test_validate_identifier_rejects(value: str) -> None:
    with pytest.raises(ValueError):
        validate_identifier(value, field="tenant_id")


def test_principal_dedups_and_sorts_groups() -> None:
    p = Principal(tenant_id="t1", group_ids=("b", "a", "b"))
    assert p.group_ids == ("a", "b")


def test_principal_caps_groups_before_dedup() -> None:
    with pytest.raises(ValidationError):
        Principal(tenant_id="t1", group_ids=("g",) * 101)


def test_principal_rejects_invalid_group() -> None:
    with pytest.raises(ValidationError):
        Principal(tenant_id="t1", group_ids=("ok", "not ok"))


def test_principal_rejects_invalid_tenant() -> None:
    with pytest.raises(ValidationError):
        Principal(tenant_id="", group_ids=())


def test_principal_is_frozen() -> None:
    p = Principal(tenant_id="t1", group_ids=())
    with pytest.raises(ValidationError):
        p.tenant_id = "t2"  # type: ignore[misc]
