from fastapi.testclient import TestClient

from azgenai_lab.main import app


def before_scenario(context, scenario):  # type: ignore[no-untyped-def]
    context.client = TestClient(app)
    context.response = None
