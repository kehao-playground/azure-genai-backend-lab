import os

# Must run before the app import below: the chat, search, and embedding
# clients are all built at import time via build_rag_service (fail fast in
# production), and the test suite must never depend on the local .env or
# shell environment (review r01 fix 2; extended to search/embeddings for
# Day 14 review finding 3). Each of these three flags is owned by the test
# suite, not the developer's shell — pin all three.
os.environ["USE_FAKE_LLM"] = "true"
os.environ["USE_FAKE_SEARCH"] = "true"
os.environ["USE_FAKE_EMBEDDINGS"] = "true"

from collections.abc import Generator  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from azgenai_lab.core.config import get_settings  # noqa: E402
from azgenai_lab.main import app  # noqa: E402
from azgenai_lab.services.conversation import build_conversation_service  # noqa: E402
from azgenai_lab.services.rag import build_rag_service  # noqa: E402

# Day 15: the four protected endpoints (chat, chat/stream, rag, agent) now 401
# without identity headers. Every existing unit test exercises the happy
# path through the shared `client` fixture, so its default headers carry
# valid identity — tests of the 401 boundary itself use their own
# TestClient (see test_auth_endpoints.py) so they can omit or corrupt these.
# Day 19: X-User-Id joins X-Tenant-Id as a required header.
AUTH_HEADERS = {"X-Tenant-Id": "t1", "X-User-Id": "u1"}


@pytest.fixture
def client() -> Generator[TestClient]:
    with TestClient(app, headers=AUTH_HEADERS) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    # The app is module-level; rebuild its state so conversations never leak
    # from one test into the next.
    from azgenai_lab.core.keyed_lock import KeyedLock
    from azgenai_lab.services.agent_turn import build_agent_turn_service
    from azgenai_lab.services.conversation_store import build_conversation_store

    settings = get_settings()
    store = build_conversation_store(settings)
    locks: KeyedLock[tuple[str, str]] = KeyedLock()
    app.state.conversation_service = build_conversation_service(
        settings, store=store, locks=locks
    )
    app.state.rag_service = build_rag_service(settings)
    app.state.agent_turn_service = build_agent_turn_service(
        settings, store=store, locks=locks
    )
