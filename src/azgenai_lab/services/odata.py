"""OData primitives shared by search boundary modules.

Kept deliberately small: this module owns escaping alone. Anything that
constructs a full filter expression (ACL policy, enumeration paging, ...)
belongs to the module that owns that expression's semantics, not here.
"""


def escape_odata_literal(value: str) -> str:
    """Escape a value for an OData string literal (a single quote doubles)."""
    return value.replace("'", "''")


__all__ = ["escape_odata_literal"]
