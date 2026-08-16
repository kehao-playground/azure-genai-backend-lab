"""resolve_aoai_auth: one place that turns settings into what an AsyncOpenAI
construction site needs — the api_key argument (a static string in api_key
mode, or a per-request async bearer callable in entra mode, Day 20 ruling:
explicit ManagedIdentityCredential) and a uniform ``aclose()`` to release
whatever that mode minted (the entra credential's own ``close``, or a no-op
in api_key mode — a uniform closer removes a `None`-check from every caller).

The provider must be *async*: the pinned openai SDK's AsyncOpenAI awaits it
unconditionally on every request (tests/unit/test_openai_callable_api_key.py),
so this resolver uses azure.identity.aio, not the sync azure.identity."""

import inspect
from collections.abc import Awaitable, Callable

import pytest

from azgenai_lab.core.config import Settings
from azgenai_lab.services.azure_openai_auth import COGNITIVE_SERVICES_SCOPE, resolve_aoai_auth


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "use_fake_llm": False,
        "azure_openai_endpoint": "https://example.openai.azure.com",
        "azure_openai_api_key": "k",
        "azure_openai_deployment_name": "chat-mini",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_api_key_mode_returns_secret_string() -> None:
    auth = resolve_aoai_auth(_settings())
    assert auth.api_key == "k"


async def test_api_key_mode_aclose_is_a_safe_no_op() -> None:
    auth = resolve_aoai_auth(_settings())
    # Nothing was minted; awaiting it must not raise.
    assert await auth.aclose() is None


def test_api_key_mode_missing_key_raises() -> None:
    with pytest.raises(ValueError, match="AZURE_OPENAI_AUTH=api_key requires AZURE_OPENAI_API_KEY"):
        resolve_aoai_auth(_settings(azure_openai_api_key=None))


def test_entra_mode_missing_client_id_fails_fast() -> None:
    with pytest.raises(ValueError, match="AZURE_OPENAI_AUTH=entra requires AZURE_CLIENT_ID"):
        resolve_aoai_auth(_settings(azure_openai_auth="entra", azure_openai_api_key=None))


async def test_entra_bearer_without_minting(monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, object] = {}
    token_calls: list[str] = []

    class FakeCredential:
        def __init__(self, client_id: str) -> None:
            created["client_id"] = client_id
            self.close_count = 0
            created["credential"] = self

        async def close(self) -> None:
            self.close_count += 1

    def fake_provider(credential: object, scope: str) -> Callable[[], Awaitable[str]]:
        created["scope"] = scope

        async def token_callable() -> str:
            token_calls.append("called")
            return "tok"

        return token_callable

    monkeypatch.setattr(
        "azgenai_lab.services.azure_openai_auth.ManagedIdentityCredential", FakeCredential
    )
    monkeypatch.setattr(
        "azgenai_lab.services.azure_openai_auth.get_bearer_token_provider", fake_provider
    )
    auth = resolve_aoai_auth(
        _settings(azure_openai_auth="entra", azure_openai_api_key=None, azure_client_id="cid")
    )
    key = auth.api_key
    assert callable(key)
    # The pinned SDK's AsyncOpenAI awaits this callable's result on every
    # request (test_openai_callable_api_key.py) — a sync callable would
    # crash at the first real request, so the resolver must hand back an
    # async one, not merely "some callable".
    assert inspect.iscoroutinefunction(key)
    assert created["client_id"] == "cid"
    assert created["scope"] == COGNITIVE_SERVICES_SCOPE
    # Verify the token callable was NOT invoked at construction time (no minting)
    assert token_calls == [], "token provider should not be called during resolve_aoai_auth"
    # Verify it IS callable and returns the token when invoked
    assert await key() == "tok"
    assert token_calls == ["called"], "token provider should be called exactly once"
    # `auth.aclose` is the credential's own close, not a separately-invented
    # closer: awaiting it must close the credential exactly once.
    credential = created["credential"]
    assert isinstance(credential, FakeCredential)
    await auth.aclose()
    assert credential.close_count == 1
