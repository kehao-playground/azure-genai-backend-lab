from fastapi.testclient import TestClient

from azgenai_lab.main import app
from azgenai_lab.services.azure_openai import FakeChatService, get_chat_service


def before_scenario(context, scenario):  # type: ignore[no-untyped-def]
    # BDD contract runs always use the fake adapter, whatever the local .env says.
    app.dependency_overrides[get_chat_service] = FakeChatService
    context.client = TestClient(app)
    context.response = None


def after_scenario(context, scenario):  # type: ignore[no-untyped-def]
    app.dependency_overrides.clear()
