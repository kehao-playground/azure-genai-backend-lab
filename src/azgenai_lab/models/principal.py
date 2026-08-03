"""Validated authorization context for a request.

The Principal is the boundary object formed either from trusted gateway headers
(Day 15) or from the claims of a verified Entra ID access token (Day 19),
whichever adapter ``AUTH_MODE`` selected at startup. Validation lives on the
model itself so the normal validation path cannot produce an illegal Principal;
``model_construct()`` deliberately bypasses it and is reserved for
defense-in-depth tests.
"""

import re

from pydantic import BaseModel, ConfigDict, field_validator

IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_GROUPS = 100


def validate_identifier(value: str, *, field: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field} must match [A-Za-z0-9_-] and be 1-64 characters")
    return value


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    user_id: str
    group_ids: tuple[str, ...]

    @field_validator("tenant_id")
    @classmethod
    def _valid_tenant(cls, v: str) -> str:
        return validate_identifier(v, field="tenant_id")

    @field_validator("user_id")
    @classmethod
    def _valid_user(cls, value: str) -> str:
        return validate_identifier(value, field="user_id")

    @field_validator("group_ids")
    @classmethod
    def _valid_groups(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(v) > MAX_GROUPS:
            raise ValueError(f"at most {MAX_GROUPS} group_ids allowed")
        for g in v:
            validate_identifier(g, field="group_id")
        return tuple(sorted(set(v)))
