from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from azgenai_lab.api.chat import get_chat_service
from azgenai_lab.main import app
from azgenai_lab.services.azure_openai import FakeChatService


@pytest.fixture
def client() -> Generator[TestClient]:
    # Tests always run against the fake adapter, whatever the local .env says.
    app.dependency_overrides[get_chat_service] = FakeChatService
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
