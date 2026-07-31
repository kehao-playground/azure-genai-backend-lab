"""Who allocated the connection pool decides who may close it.

Both search adapters accept an injected ``httpx.AsyncClient`` and fall back to
building their own. Only the second kind is theirs to shut down: closing an
injected pool reaches past the adapter's own lifetime into whatever else is
sharing it, and the symptom — a client that was fine a moment ago now raising
on every request — surfaces far from the line that caused it.

Reading ``is_closed`` off the underlying client is the only way to observe
either branch: an adapter that never closes what it made and one that closes
everything it is handed are indistinguishable from the outside.
"""

import httpx
import pytest
from pydantic import SecretStr

from azgenai_lab.core.config import Settings
from azgenai_lab.models.search import SearchMode
from azgenai_lab.services.azure_search import AzureSearchClient
from azgenai_lab.services.search_data_plane import SearchDataPlane


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        azure_search_endpoint="https://example.search.windows.net",
        azure_search_admin_key=SecretStr("k"),
        use_fake_search=False,
    )


def _injected() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": []})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_the_query_adapter_closes_the_pool_it_allocated() -> None:
    client = AzureSearchClient(_settings())
    async with client:
        pass
    assert client._client.is_closed is True


async def test_the_query_adapter_leaves_an_injected_pool_open() -> None:
    injected = _injected()
    async with AzureSearchClient(_settings(), client=injected):
        pass

    assert injected.is_closed is False
    # Still usable, which is the property the caller actually cares about —
    # `is_closed` alone would not catch a pool left in some other broken state.
    reused = AzureSearchClient(_settings(), client=injected)
    assert (await reused.search("q", mode=SearchMode.KEYWORD, top=5)).hits == ()
    await injected.aclose()


async def test_the_data_plane_closes_the_pool_it_allocated() -> None:
    plane = SearchDataPlane(_settings())
    async with plane:
        pass
    assert plane._client.is_closed is True


async def test_the_data_plane_leaves_an_injected_pool_open() -> None:
    injected = _injected()
    async with SearchDataPlane(_settings(), client=injected):
        pass

    assert injected.is_closed is False
    reused = SearchDataPlane(_settings(), client=injected)
    assert await reused.list_chunk_ids("t", "doc") == []
    await injected.aclose()


@pytest.mark.parametrize("build", [AzureSearchClient, SearchDataPlane])
async def test_closing_twice_is_not_an_error(build: type) -> None:
    # A tool that closes explicitly and then leaves an `async with` block would
    # otherwise fail on the way out, after all its real work succeeded.
    adapter = build(_settings())
    await adapter.aclose()
    await adapter.aclose()


async def test_an_exception_inside_the_block_still_closes_the_pool() -> None:
    client = AzureSearchClient(_settings())
    with pytest.raises(RuntimeError):
        async with client:
            raise RuntimeError("the run aborted")
    assert client._client.is_closed is True
