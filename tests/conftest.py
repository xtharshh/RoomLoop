from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from roomloop.api import create_app


@pytest.fixture()
def client() -> TestClient:
    app = create_app(":memory:")
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def service(client: TestClient):
    return client.app.state.service


@pytest.fixture(autouse=True)
def _no_demo_env(monkeypatch):
    # Keep the demo reset endpoint deterministic inside tests.
    monkeypatch.setenv("ROOMLOOP_DEMO", "0")
