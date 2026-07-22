import pytest
from pydantic import ValidationError

from azgenai_lab.core.config import Settings


def test_default_settings_use_fake_services() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_name == "azure-genai-backend-lab"
    assert settings.use_fake_llm is True
    assert settings.use_fake_search is True


@pytest.mark.parametrize("value", [0, -1])
def test_llm_max_output_tokens_rejects_non_positive(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_max_output_tokens=value)


@pytest.mark.parametrize("value", [0, -1])
def test_conversation_token_budget_rejects_non_positive(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, conversation_token_budget=value)


def test_conversation_token_budget_none_disables() -> None:
    settings = Settings(_env_file=None, conversation_token_budget=None)
    assert settings.conversation_token_budget is None
