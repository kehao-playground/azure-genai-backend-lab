"""Pinned-SDK regression: a callable api_key is a per-request refresh seam.

docs/managed-identity.md tells readers to hand the azure-identity bearer-token
provider to the client as the callable itself (``api_key=token_provider``),
never its result (``api_key=token_provider()``). That advice holds only while
the pinned ``openai`` package (a) accepts a callable and (b) re-invokes it on
each request, so a long-running service picks up refreshed tokens after the
first one expires. Both facts are SDK behavior, not our code — this test
freezes them so an SDK upgrade that breaks either fails here before the doc
goes stale (Day 20 review, finding R1).
"""

import httpx
from openai import OpenAI

EMPTY_LIST_BODY = {"object": "list", "data": []}


def _client_with(api_key: object, seen_auth: list[str | None]) -> OpenAI:
    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("Authorization"))
        return httpx.Response(200, json=EMPTY_LIST_BODY)

    return OpenAI(
        base_url="http://testserver/v1",
        api_key=api_key,  # type: ignore[arg-type]  # str | Callable per pinned SDK
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_callable_api_key_is_invoked_per_request_not_frozen() -> None:
    minted: list[str] = []

    def provider() -> str:
        minted.append(f"token-{len(minted) + 1}")
        return minted[-1]

    seen_auth: list[str | None] = []
    client = _client_with(provider, seen_auth)

    # Lazy at construction: no token is minted until a request needs one.
    assert minted == []

    client.models.list()
    client.models.list()

    # Re-invoked per request: each call carries a freshly minted token.
    assert seen_auth == ["Bearer token-1", "Bearer token-2"]


def test_eagerly_called_provider_freezes_one_token() -> None:
    # The shape the docs warn against: api_key=token_provider() passes a
    # static string, and every later request replays it forever.
    seen_auth: list[str | None] = []
    client = _client_with("token-frozen", seen_auth)

    client.models.list()
    client.models.list()

    assert seen_auth == ["Bearer token-frozen", "Bearer token-frozen"]
