import json

from azgenai_lab.models.chat import Message
from azgenai_lab.models.principal import Principal
from azgenai_lab.services.agent_tools import make_get_conversation_usage
from azgenai_lab.services.conversation_store import InMemoryConversationStore

OPS = Principal(tenant_id="opsdemo", user_id="u1", group_ids=())
NOT_FOUND = {
    "found": False, "spent_tokens": None, "budget_tokens": None,
    "remaining_tokens": None, "budget_state": None,
}


async def _store_with(tenant: str, cid: str, tokens: int) -> InMemoryConversationStore:
    store = InMemoryConversationStore()
    await store.append(tenant, cid, turns=[], replay_items=[], expected_revision=0,
                       usage_tokens=tokens, first_turn_authorization_group_ids=())
    return store


async def test_states_and_clamp() -> None:
    for spent, state, remaining in [
        (100, "fresh", 300), (320, "near_exhausted", 80),
        (400, "exhausted", 0), (450, "exhausted", 0),  # post-paid overshoot clamps
    ]:
        store = await _store_with("opsdemo", "c1", spent)
        tool = make_get_conversation_usage(store, OPS, token_budget=400)
        payload = json.loads(await tool("c1"))
        assert payload == {
            "found": True, "spent_tokens": spent, "budget_tokens": 400,
            "remaining_tokens": remaining, "budget_state": state,
        }


async def test_unknown_and_cross_tenant_are_identical_not_found() -> None:
    store = await _store_with("acme", "c1", 100)  # exists, wrong tenant
    tool = make_get_conversation_usage(store, OPS, token_budget=400)
    assert json.loads(await tool("c1")) == NOT_FOUND       # cross-tenant
    assert json.loads(await tool("nope")) == NOT_FOUND     # unknown


async def test_usage_scope_mismatch_is_not_found_shape() -> None:
    store = InMemoryConversationStore()
    await store.append(
        "t1", "c1", [Message(role="user", content="hi")], [], 0, 100,
        first_turn_authorization_group_ids=("g1",),
    )
    tool = make_get_conversation_usage(
        store, Principal(tenant_id="t1", user_id="u1", group_ids=("g2",)), 50_000
    )
    result = json.loads(await tool(conversation_id="c1"))
    assert result["found"] is False
    assert result["spent_tokens"] is None
