"""AOAI auth resolution: the composition-point seam for Day 24 keyless.

``api_key`` mode returns the configured secret string (existing behaviour),
``aclose`` a no-op. ``entra`` mode returns azure-identity's bearer-token
provider *as the callable itself* — the pinned openai SDK re-invokes a
callable api_key per request (tests/unit/test_openai_callable_api_key.py).
That per-request re-invocation guarantee is proven on the sync
``openai.OpenAI`` client, but this app builds ``openai.AsyncOpenAI``
exclusively, and the pinned SDK's async client *awaits* the provider's
return value (``self.api_key = await self._api_key_provider()``)
unconditionally — a sync callable returning ``str`` raises ``TypeError:
object str can't be used in 'await' expression'`` on the first request. The
sync ``azure.identity`` credential/provider pair used to satisfy the sync
client; this app needs the ``azure.identity.aio`` pair instead, whose
provider is ``Callable[[], Coroutine[Any, Any, str]]``. Explicit
``ManagedIdentityCredential(client_id=...)`` per the Day 20 ruling
(docs/managed-identity.md §3): this is a fixed production path, not a
cross-environment chain.

The ``azure.identity.aio`` credential needs an async HTTP transport, and
``azure-core``'s only async transport besides a thread-wrapped sync one is
``AioHttpTransport`` — which requires the ``aiohttp`` package. ``aiohttp`` is
a direct dependency of this project *because of that* even though nothing
here imports it by name; it is pulled in transitively the moment
``ManagedIdentityCredential`` builds its pipeline. Do not remove it as
"unused" without re-checking this.

The credential owns that transport's session and must be closed
(``await credential.close()``) or it leaks an aiohttp ``ClientSession``,
surfacing as ``ResourceWarning: Unclosed client session`` at teardown — this
only appears once the provider has actually been invoked (the session opens
lazily on first request), so a leak here is silent until a real call has
gone through. Rather than have this module own that lifecycle, :class:`AoaiAuth`
hands back a uniform ``aclose`` callable (the entra credential's own
``close``, or a no-op in api_key mode) so each of the three real adapters can
await it from its own existing ``aclose()``, alongside the ``AsyncOpenAI``
client it already owns — the same one-transport-per-adapter pattern already
in use, not a new one. A uniform closer beats an ``Optional[credential]``
field: it removes a ``None``-check from three separate call sites, each of
which would otherwise be a chance to forget it and leak silently.

That is a deliberate choice, not an oversight: it costs three separate
IMDS-backed credentials/sessions (one per adapter) instead of a single shared
one when all three real adapters are built together, in exchange for not
threading a new shared resource through composition (``main.py``'s
four-closer chain stays untouched).
"""

from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from azure.identity.aio import ManagedIdentityCredential, get_bearer_token_provider

from azgenai_lab.core.config import Settings

COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"


async def _no_credential_to_close() -> None:
    """api_key mode's aclose: nothing was minted, so there is nothing to
    release. Exists so callers can always await ``auth.aclose()`` rather
    than branch on whether a credential exists."""
    return None


@dataclass(frozen=True)
class AoaiAuth:
    """What a construction site needs: the ``api_key`` argument for
    ``AsyncOpenAI``, and an ``aclose()`` to await from its own ``aclose()`` —
    the entra credential's ``close`` in entra mode, a no-op in api_key mode."""

    api_key: str | Callable[[], Awaitable[str]]
    # Coroutine, not the broader Awaitable: asyncio.Task.create_task (used in
    # agent_framework.py's partial-construction guard) requires a Coroutine
    # specifically, and every real value here (credential.close,
    # _no_credential_to_close) genuinely is one.
    aclose: Callable[[], Coroutine[Any, Any, None]]


def resolve_aoai_auth(settings: Settings) -> AoaiAuth:
    if settings.azure_openai_auth == "entra":
        if not settings.azure_client_id:
            raise ValueError("AZURE_OPENAI_AUTH=entra requires AZURE_CLIENT_ID")
        credential = ManagedIdentityCredential(client_id=settings.azure_client_id)
        provider = get_bearer_token_provider(credential, COGNITIVE_SERVICES_SCOPE)
        return AoaiAuth(api_key=provider, aclose=credential.close)
    if not settings.azure_openai_api_key:
        raise ValueError("AZURE_OPENAI_AUTH=api_key requires AZURE_OPENAI_API_KEY")
    return AoaiAuth(
        api_key=settings.azure_openai_api_key.get_secret_value(),
        aclose=_no_credential_to_close,
    )
