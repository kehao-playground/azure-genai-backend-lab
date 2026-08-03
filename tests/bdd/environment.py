import os

# Must run before the app import below: BDD contract runs never depend on the
# local .env or shell environment (review r01 fix 2). The app builds the
# chat, search, and embedding clients at import time via build_rag_service,
# so all three fake-adapter flags are owned by the test suite here, not the
# developer's shell (Day 14 review finding 3).
os.environ["USE_FAKE_LLM"] = "true"
os.environ["USE_FAKE_SEARCH"] = "true"
os.environ["USE_FAKE_EMBEDDINGS"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from azgenai_lab.core.config import get_settings  # noqa: E402
from azgenai_lab.main import app  # noqa: E402
from azgenai_lab.services.conversation import build_conversation_service  # noqa: E402
from azgenai_lab.services.rag import build_rag_service  # noqa: E402

# Day 15: the three protected endpoints now 401 without identity headers;
# scenarios exercise the documented contract, not the auth boundary itself
# (that lives in tests/unit/test_auth_endpoints.py), so the shared client
# carries valid identity headers by default.
AUTH_HEADERS = {"X-Tenant-Id": "t1"}


def before_scenario(context, scenario):  # type: ignore[no-untyped-def]
    context.client = TestClient(app, headers=AUTH_HEADERS)
    context.response = None


def after_scenario(context, scenario):  # type: ignore[no-untyped-def]
    app.dependency_overrides.clear()
    # The app is module-level; rebuild its state so conversations never leak
    # from one scenario into the next.
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
