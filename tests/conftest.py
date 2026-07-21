import os

# Must run before the app import below: the chat service is built at import
# time (fail fast in production) and the test suite must never depend on the
# local .env or shell environment (review r01 fix 2).
os.environ["USE_FAKE_LLM"] = "true"

from collections.abc import Generator  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from azgenai_lab.core.config import get_settings  # noqa: E402
from azgenai_lab.main import app  # noqa: E402
from azgenai_lab.services.conversation import build_conversation_service  # noqa: E402


@pytest.fixture
def client() -> Generator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    # The app is module-level; rebuild its state so conversations never leak
    # from one test into the next.
    app.state.conversation_service = build_conversation_service(get_settings())
