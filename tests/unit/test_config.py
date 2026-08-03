import pytest
from pydantic import SecretStr, ValidationError

from azgenai_lab.core.config import Settings
from azgenai_lab.models.search_index import INDEX_NAME


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


def test_index_name_is_not_a_setting() -> None:
    # A setting is precisely the mechanism for letting two things that must
    # agree disagree at runtime — which is how .env.example came to publish
    # "documents" against a constant of "azgenai-lab-chunks".
    assert "azure_search_index_name" not in Settings.model_fields
    assert INDEX_NAME == "azgenai-lab-chunks"


def test_admin_key_is_a_secret() -> None:
    settings = Settings(_env_file=None, azure_search_admin_key=SecretStr("super-secret"))
    assert settings.azure_search_admin_key is not None
    assert "super-secret" not in repr(settings)
    assert settings.azure_search_admin_key.get_secret_value() == "super-secret"


def test_rag_top_default_and_validation() -> None:
    assert Settings(_env_file=None).rag_top == 5
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rag_top=0)


def test_rag_top_upper_bound() -> None:
    # Day 14 review finding 2: rag_top had no upper bound, so a value like
    # 1001 passed settings validation and only failed later, as a
    # plain-text 500, when it hit models/search.py's MAX_TOP=1000 check at
    # request time. 50 = DEFAULT_VECTOR_K, the vector leg's candidate pool.
    assert Settings(_env_file=None, rag_top=50).rag_top == 50
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rag_top=51)


def test_agent_limits_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.agent_max_iterations == 5
    assert settings.agent_max_tool_calls == 10


@pytest.mark.parametrize("field", ["agent_max_iterations", "agent_max_tool_calls"])
@pytest.mark.parametrize("bad", [0, -1])
def test_agent_limits_must_be_positive(field: str, bad: int) -> None:
    with pytest.raises(ValidationError):
        Settings(**{"_env_file": None, field: bad})


TENANT_ID = "11111111-1111-1111-1111-111111111111"
AUDIENCE = "22222222-2222-2222-2222-222222222222"


def _entra_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "auth_mode": "entra",
        "entra_tenant_id": TENANT_ID.upper(),
        "entra_audience": AUDIENCE.upper(),
        "entra_required_scope": " access_as_user ",
    }
    values.update(overrides)
    return Settings(**values)


def test_auth_defaults_to_headers() -> None:
    assert Settings(_env_file=None).auth_mode == "headers"


def test_entra_ids_and_permissions_are_normalized() -> None:
    settings = _entra_settings()
    assert settings.entra_tenant_id == TENANT_ID
    assert settings.entra_audience == AUDIENCE
    assert settings.entra_required_scope == "access_as_user"
    assert settings.entra_required_app_role is None


@pytest.mark.parametrize("field", ["entra_tenant_id", "entra_audience"])
@pytest.mark.parametrize("bad", [None, "", "not-a-guid"])
def test_entra_requires_guid_tenant_and_audience(field: str, bad: object) -> None:
    with pytest.raises(ValidationError):
        _entra_settings(**{field: bad})


def test_entra_requires_scope_or_app_role() -> None:
    with pytest.raises(ValidationError):
        _entra_settings(entra_required_scope=" ", entra_required_app_role="")


def test_entra_accepts_role_only() -> None:
    settings = _entra_settings(
        entra_required_scope=None,
        entra_required_app_role=" Api.Access ",
    )
    assert settings.entra_required_scope is None
    assert settings.entra_required_app_role == "Api.Access"
