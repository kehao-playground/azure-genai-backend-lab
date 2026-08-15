"""resolve_api_key: one place that turns settings into the AsyncOpenAI api_key
argument — a static string (api_key mode) or a per-request bearer callable
(entra mode, Day 20 ruling: explicit ManagedIdentityCredential)."""

import pytest

from azgenai_lab.core.config import Settings
from azgenai_lab.services.azure_openai_auth import COGNITIVE_SERVICES_SCOPE, resolve_api_key


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
    assert resolve_api_key(_settings()) == "k"


def test_api_key_mode_missing_key_raises() -> None:
    with pytest.raises(ValueError, match="AZURE_OPENAI_AUTH=api_key requires AZURE_OPENAI_API_KEY"):
        resolve_api_key(_settings(azure_openai_api_key=None))


def test_entra_mode_missing_client_id_fails_fast() -> None:
    with pytest.raises(ValueError, match="AZURE_OPENAI_AUTH=entra requires AZURE_CLIENT_ID"):
        resolve_api_key(_settings(azure_openai_auth="entra", azure_openai_api_key=None))


def test_entra_bearer_without_minting(monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, object] = {}

    class FakeCredential:
        def __init__(self, client_id: str) -> None:
            created["client_id"] = client_id

    def fake_provider(credential: object, scope: str):  # noqa: ANN202
        created["scope"] = scope
        return lambda: "tok"

    monkeypatch.setattr(
        "azgenai_lab.services.azure_openai_auth.ManagedIdentityCredential", FakeCredential
    )
    monkeypatch.setattr(
        "azgenai_lab.services.azure_openai_auth.get_bearer_token_provider", fake_provider
    )
    key = resolve_api_key(_settings(azure_openai_auth="entra", azure_openai_api_key=None,
                                    azure_client_id="cid"))
    assert callable(key)
    assert created == {"client_id": "cid", "scope": COGNITIVE_SERVICES_SCOPE}
    assert key() == "tok"
