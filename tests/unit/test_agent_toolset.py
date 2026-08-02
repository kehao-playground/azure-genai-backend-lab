import json

from azgenai_lab.core.config import Settings
from azgenai_lab.models.principal import Principal
from azgenai_lab.models.search_index import EMBEDDING_DIMENSIONS
from azgenai_lab.services.agent_tools import build_agent_toolset
from azgenai_lab.services.conversation_store import InMemoryConversationStore
from azgenai_lab.services.embeddings import FakeEmbeddingClient

OPS = Principal(tenant_id="opsdemo", group_ids=())


def _settings() -> Settings:
    """Settings isolated from the repo-root `.env`.

    `conftest.py` pins only the three fake-adapter flags, so every other
    field is still ambient: a developer's untracked `.env` would otherwise
    reach these assertions, and no fresh clone or CI checkout would
    reproduce the failure (the same hazard `tests/unit/test_config.py`
    avoids with `_env_file=None`).
    """
    return Settings(_env_file=None)


# The format `make_parent_id` derives a key with (models/rag.py): everything
# before the doc-id length digit is fixed by the tenant id alone, so this
# prefix identifies every parent id that belongs to the "opsdemo" tenant
# without guessing at or duplicating the doc-id half of the encoding.
_OPSDEMO_PARENT_PREFIX = f"t{len(OPS.tenant_id)}={OPS.tenant_id}d"


async def test_fake_toolset_search_finds_ops_corpus() -> None:
    # use_fake_search default builds an *empty* FakeSearchClient; the toolset
    # must seed it with the real corpus so fake-mode demos ground on documents.
    toolset = build_agent_toolset(
        _settings(), OPS, conversation_store=InMemoryConversationStore(), token_budget=400
    )
    try:
        search_docs = toolset.tools[0]
        payload = json.loads(await search_docs("token budget exceeded 429"))
        assert payload["hits"], "seeded fake search must return ops-corpus hits"
        assert any("token-budget" in h["source"] for h in payload["hits"])
    finally:
        await toolset.retriever.aclose()


async def test_fake_toolset_search_hits_are_scoped_to_the_caller_tenant() -> None:
    # Seeding loads every sample tenant's corpus into the shared fake index
    # (agent_tools.py's `_seed_index_documents` docstring), so this pins that
    # the ACL check `FakeSearchClient.search` performs actually keeps a
    # cross-tenant document out of an opsdemo caller's results — otherwise an
    # ACL regression here would only fail by accident.
    toolset = build_agent_toolset(
        _settings(), OPS, conversation_store=InMemoryConversationStore(), token_budget=400
    )
    try:
        search_docs = toolset.tools[0]
        payload = json.loads(await search_docs("token budget exceeded 429"))
        assert payload["hits"]
        for hit in payload["hits"]:
            assert hit["source"].startswith(_OPSDEMO_PARENT_PREFIX), (
                f"hit source {hit['source']!r} is not scoped to tenant {OPS.tenant_id!r}"
            )
    finally:
        await toolset.retriever.aclose()


async def test_toolset_shares_the_supplied_store() -> None:
    store = InMemoryConversationStore()
    toolset = build_agent_toolset(_settings(), OPS, conversation_store=store, token_budget=400)
    try:
        # Identity check: the toolset reports back the caller's store object.
        assert toolset.conversation_store is store

        # Behavioral check: the store the usage *tool* actually closed over
        # is the same one, not merely the one the dataclass field reports. A
        # `make_get_conversation_usage` call wired to a different, freshly
        # built store would still pass the identity assertion above while
        # silently making a seeded conversation invisible to the agent.
        await store.append(
            OPS.tenant_id, "seeded-convo", turns=[], replay_items=[],
            expected_revision=0, usage_tokens=123,
        )
        get_conversation_usage = toolset.tools[2]
        payload = json.loads(await get_conversation_usage("seeded-convo"))
        assert payload["found"] is True
        assert payload["spent_tokens"] == 123
    finally:
        await toolset.retriever.aclose()


async def test_toolset_runtime_config_and_usage_report_the_same_budget() -> None:
    # Nothing else pins that `token_budget` reaches both tools as the same
    # number: `build_agent_toolset` passes one value to both
    # `make_get_runtime_config` and `make_get_conversation_usage`, and a
    # future edit could thread a different value to one of them without any
    # existing test noticing.
    store = InMemoryConversationStore()
    await store.append(
        OPS.tenant_id, "c1", turns=[], replay_items=[], expected_revision=0, usage_tokens=10
    )
    toolset = build_agent_toolset(_settings(), OPS, conversation_store=store, token_budget=777)
    try:
        get_runtime_config, get_conversation_usage = toolset.tools[1], toolset.tools[2]
        config_payload = json.loads(await get_runtime_config())
        usage_payload = json.loads(await get_conversation_usage("c1"))
        assert config_payload["conversation_token_budget"] == 777
        assert usage_payload["budget_tokens"] == 777
        assert config_payload["conversation_token_budget"] == usage_payload["budget_tokens"]
    finally:
        await toolset.retriever.aclose()


async def test_seeded_vectors_match_the_async_embedding_path_exactly() -> None:
    # `_seeded_fake_retriever` builds its vectors through
    # `FakeEmbeddingClient.pseudo_vector` directly (a plain sync loop, no
    # thread, no nested event loop). This pins that those vectors are
    # identical in both length and content to what the async `embed` path
    # produces for the same text, so the dimension contract the removed
    # thread bridge used to satisfy still holds under the synchronous
    # seeding path — not merely "some list of the right length", but the
    # same vector `embed_chunks` would have returned.
    client = FakeEmbeddingClient()
    text = "some chunk text"
    sync_vector = client.pseudo_vector(text)
    async_vector = (await client.embed([text]))[0]
    assert len(sync_vector) == EMBEDDING_DIMENSIONS
    assert sync_vector == async_vector
