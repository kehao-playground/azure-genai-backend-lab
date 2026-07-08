from dataclasses import dataclass


@dataclass(frozen=True)
class UserContext:
    user_id: str
    tenant_id: str | None = None


def get_dev_user_context() -> UserContext:
    return UserContext(user_id="local-dev-user", tenant_id="local-dev-tenant")
