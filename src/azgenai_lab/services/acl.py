"""Single-source ACL policy: filter construction, visibility check, metadata contract.

Binding semantics (do not duplicate this decision elsewhere):

- Same tenant is always required.
- ``allowed_groups == []`` means the document is tenant-wide readable.
- A non-empty ``allowed_groups`` requires any intersection with the
  principal's groups.
- There is no such thing as a global-public document (no tenant means
  "readable by nobody", not "readable by everybody").
- Document ACL metadata is a contract, not an optional hint: a missing key
  or a wrong type is a programming/data error (``ValueError``), never
  defaulted to public. ``.get("allowed_groups", [])`` is forbidden for this
  reason — a silently-defaulted empty list is indistinguishable from a
  legitimately tenant-wide document.
"""

from collections.abc import Mapping
from typing import Any

from azgenai_lab.models.principal import Principal
from azgenai_lab.services.odata import escape_odata_literal


def require_acl_metadata(document: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    """Read and type-check ``tenant_id``/``allowed_groups`` off a document.

    Raises ``ValueError`` if either key is missing or is not the expected
    type. This is the only place allowed to read these two fields off a raw
    document mapping.
    """
    if "tenant_id" not in document:
        raise ValueError("document is missing required field 'tenant_id'")
    if "allowed_groups" not in document:
        raise ValueError("document is missing required field 'allowed_groups'")

    tenant_id = document["tenant_id"]
    if not isinstance(tenant_id, str):
        raise ValueError("document field 'tenant_id' must be a string")

    allowed_groups = document["allowed_groups"]
    if not isinstance(allowed_groups, list) or not all(
        isinstance(group, str) for group in allowed_groups
    ):
        raise ValueError("document field 'allowed_groups' must be a list of strings")

    return tenant_id, tuple(allowed_groups)


def build_acl_filter(principal: Principal) -> str:
    """Build the OData filter expression enforcing this principal's ACL scope."""
    tenant_clause = f"tenant_id eq '{escape_odata_literal(principal.tenant_id)}'"
    if not principal.group_ids:
        return f"{tenant_clause} and not allowed_groups/any()"

    joined = escape_odata_literal(",".join(sorted(principal.group_ids)))
    groups_clause = (
        "(not allowed_groups/any() "
        f"or allowed_groups/any(g: search.in(g, '{joined}')))"
    )
    return f"{tenant_clause} and {groups_clause}"


def is_document_visible(document: Mapping[str, Any], principal: Principal) -> bool:
    """Whether ``principal`` may see ``document`` under the ACL policy above."""
    tenant_id, allowed_groups = require_acl_metadata(document)
    if tenant_id != principal.tenant_id:
        return False
    if not allowed_groups:
        return True
    return not set(allowed_groups).isdisjoint(principal.group_ids)


__all__ = ["build_acl_filter", "is_document_visible", "require_acl_metadata"]
