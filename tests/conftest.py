from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from grid.server import create_app


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "grid.db")


@pytest.fixture
def api(app):
    return TestClient(app)


@dataclass
class Seed:
    human: str
    lead: str
    frontend: str
    backend: str
    reviewer: str

    @staticmethod
    def h(token):
        return {"Authorization": f"Bearer {token}"}


def _join(api, human_token, name, role, tool):
    r = api.post("/participants", json={"name": name, "role": role, "tool": tool},
                 headers=Seed.h(human_token))
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture
def seed(api):
    r = api.post("/init", json={"project": "Web app MVP", "human": "alice"})
    assert r.status_code == 200, r.text
    human = r.json()["token"]
    return Seed(
        human=human,
        lead=_join(api, human, "planner", "lead", "opus"),
        frontend=_join(api, human, "frontend", "worker", "deepseek"),
        backend=_join(api, human, "backend", "worker", "codex"),
        reviewer=_join(api, human, "checker", "reviewer", "codex"),
    )
