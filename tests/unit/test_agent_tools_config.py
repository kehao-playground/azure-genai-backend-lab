import json

from azgenai_lab.core.config import Settings
from azgenai_lab.services.agent_tools import make_get_runtime_config


async def test_runtime_config_is_allowlisted_snapshot() -> None:
    settings = Settings(azure_openai_api_key="sk-super-secret")
    tool = make_get_runtime_config(settings, demo_token_budget=400)
    payload = json.loads(await tool())
    assert payload == {
        "llm_max_output_tokens": settings.llm_max_output_tokens,
        "conversation_token_budget": 400,  # the single-source demo budget, not Settings'
        "llm_timeout_seconds": settings.llm_timeout_seconds,
        "deployment_name": settings.azure_openai_deployment_name,
        "agent_max_iterations": settings.agent_max_iterations,
        "agent_max_tool_calls": settings.agent_max_tool_calls,
    }


async def test_runtime_config_cannot_leak_secrets() -> None:
    settings = Settings(
        azure_openai_api_key="sk-super-secret", azure_search_admin_key="search-secret"
    )
    out = await make_get_runtime_config(settings, demo_token_budget=400)()
    assert "sk-super-secret" not in out
    assert "search-secret" not in out
