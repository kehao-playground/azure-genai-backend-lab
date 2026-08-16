"""Shared fakes for entra-mode AOAI credential tests (Day 24).

Was triplicated verbatim across test_azure_openai_auth.py,
test_chat_service.py, test_embeddings.py and test_agent_real_adapter.py;
consolidated here per the same convention as audit_helpers.py (Day 22),
importable via `pythonpath = ["."]`.
"""

from collections.abc import Awaitable, Callable

import pytest


class CloseTrackingCredential:
    """Stands in for `azure.identity.aio.ManagedIdentityCredential`: records
    how many times `close()` is awaited, so a test can assert exactly-once
    (or never) rather than merely that some `aclose()` call returned."""

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1


def patch_entra_credential(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Monkeypatch resolve_aoai_auth's entra-mode credential/provider pair.

    Returns a dict the caller can inspect afterward:
    - `credential`: the `CloseTrackingCredential` instance the resolver built.
    - `scope`: the scope `get_bearer_token_provider` was called with.
    - `provider`: the exact async callable `get_bearer_token_provider`
      returned — asserting a construction site's `api_key`/provider `is`
      this object (not merely that it's *some* coroutine function) pins that
      the site actually received the resolver's object, not a lookalike.
    """
    created: dict[str, object] = {}

    def fake_credential_ctor(client_id: str) -> CloseTrackingCredential:
        credential = CloseTrackingCredential(client_id)
        created["credential"] = credential
        return credential

    def fake_provider(credential: object, scope: str) -> Callable[[], Awaitable[str]]:
        created["scope"] = scope

        async def token_callable() -> str:
            return "tok"

        created["provider"] = token_callable
        return token_callable

    monkeypatch.setattr(
        "azgenai_lab.services.azure_openai_auth.ManagedIdentityCredential",
        fake_credential_ctor,
    )
    monkeypatch.setattr(
        "azgenai_lab.services.azure_openai_auth.get_bearer_token_provider", fake_provider
    )
    return created
