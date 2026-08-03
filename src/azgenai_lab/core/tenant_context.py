"""Tenant and user identity propagation for logging (Day 15; user_id added Day 19).

Mirrors ``core/correlation.py``'s ContextVar pattern: readable from anywhere
below ``require_principal`` (e.g. a log line emitted deep in a service) without
threading the tenant/user through every signature. Default ``"-"`` so the
field is always present and greppable, matching ``correlation_id``'s
convention.
"""

from contextvars import ContextVar

tenant_id_var: ContextVar[str] = ContextVar("tenant_id", default="-")
user_id_var: ContextVar[str] = ContextVar("user_id", default="-")
