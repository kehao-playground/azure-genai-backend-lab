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


def test_chunk_defaults_match_the_documented_starting_point() -> None:
    settings = Settings(_env_file=None)

    assert settings.chunk_max_chars == 2000
    assert settings.chunk_overlap_chars == 500
    assert settings.use_fake_embeddings is True
    assert settings.azure_openai_embedding_deployment is None


@pytest.mark.parametrize("value", [0, -1])
def test_chunk_max_chars_rejects_non_positive(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, chunk_max_chars=value)


def test_chunk_overlap_must_be_under_half_the_maximum() -> None:
    # The service's own text splitter imposes this; an overlap of half the
    # chunk or more means consecutive chunks stop making progress.
    Settings(_env_file=None, chunk_max_chars=1000, chunk_overlap_chars=499)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, chunk_max_chars=1000, chunk_overlap_chars=500)


def test_chunk_overlap_may_be_zero() -> None:
    assert Settings(_env_file=None, chunk_overlap_chars=0).chunk_overlap_chars == 0


@pytest.mark.parametrize("value", [-1])
def test_chunk_overlap_rejects_negative(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, chunk_overlap_chars=value)
