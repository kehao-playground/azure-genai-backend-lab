"""AOAI api_key resolution: the composition-point seam for Day 24 keyless.

``api_key`` mode returns the configured secret string (existing behaviour).
``entra`` mode returns azure-identity's bearer-token provider *as the
callable itself* — the pinned openai SDK re-invokes a callable api_key per
request (tests/unit/test_openai_callable_api_key.py), so token refresh is
the SDK's per-request call, not our plumbing. Explicit
``ManagedIdentityCredential(client_id=...)`` per the Day 20 ruling
(docs/managed-identity.md §3): this is a fixed production path, not a
cross-environment chain.
"""

from collections.abc import Callable

from azure.identity import ManagedIdentityCredential, get_bearer_token_provider

from azgenai_lab.core.config import Settings

COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"


def resolve_api_key(settings: Settings) -> str | Callable[[], str]:
    if settings.azure_openai_auth == "entra":
        if not settings.azure_client_id:
            raise ValueError("AZURE_OPENAI_AUTH=entra requires AZURE_CLIENT_ID")
        credential = ManagedIdentityCredential(client_id=settings.azure_client_id)
        return get_bearer_token_provider(credential, COGNITIVE_SERVICES_SCOPE)
    if not settings.azure_openai_api_key:
        raise ValueError("AZURE_OPENAI_AUTH=api_key requires AZURE_OPENAI_API_KEY")
    return settings.azure_openai_api_key.get_secret_value()
