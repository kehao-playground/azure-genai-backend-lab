import json

from azgenai_lab.core.config import Settings
from azgenai_lab.models.principal import Principal
from azgenai_lab.services.agent_tools import build_agent_toolset
from azgenai_lab.services.conversation_store import InMemoryConversationStore

OPS = Principal(tenant_id="opsdemo", group_ids=())


async def test_fake_toolset_search_finds_ops_corpus() -> None:
    # use_fake_search default builds an *empty* FakeSearchClient; the toolset
    # must seed it with the real corpus so fake-mode demos ground on documents.
    toolset = build_agent_toolset(
        Settings(), OPS, conversation_store=InMemoryConversationStore(), token_budget=400
    )
    try:
        search_docs = toolset.tools[0]
        payload = json.loads(await search_docs("token budget exceeded 429"))
        assert payload["hits"], "seeded fake search must return ops-corpus hits"
        assert any("token-budget" in h["source"] for h in payload["hits"])
    finally:
        await toolset.retriever.aclose()


async def test_toolset_shares_the_supplied_store() -> None:
    store = InMemoryConversationStore()
    toolset = build_agent_toolset(Settings(), OPS, conversation_store=store, token_budget=400)
    try:
        assert toolset.conversation_store is store
    finally:
        await toolset.retriever.aclose()
