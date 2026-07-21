import os

# Must run before the app import below: BDD contract runs never depend on the
# local .env or shell environment (review r01 fix 2).
os.environ["USE_FAKE_LLM"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from azgenai_lab.main import app  # noqa: E402


def before_scenario(context, scenario):  # type: ignore[no-untyped-def]
    context.client = TestClient(app)
    context.response = None


def after_scenario(context, scenario):  # type: ignore[no-untyped-def]
    app.dependency_overrides.clear()
