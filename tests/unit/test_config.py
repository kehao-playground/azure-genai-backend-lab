from azgenai_lab.core.config import Settings


def test_default_settings_use_fake_services() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_name == "azure-genai-backend-lab"
    assert settings.use_fake_llm is True
    assert settings.use_fake_search is True
