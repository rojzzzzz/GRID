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


def mk(api, seed, title, scope):
    r = api.post("/tasks", json={"title": title, "scope": scope}, headers=Seed.h(seed.lead))
    assert r.status_code == 200, r.text
    return r.json()


def assign(api, seed, task, owner, **kw):
    return api.post(f"/tasks/{task['id']}/assign", json={"owner": owner, **kw}, headers=Seed.h(seed.lead))


def send(api, token, thread, text, **fields):
    return api.post(f"/threads/{thread}/messages", json={"text": text, **fields}, headers=Seed.h(token))


def read(api, token, thread, open=False):
    params = {"open": "true"} if open else {}
    r = api.get(f"/threads/{thread}/messages", params=params, headers=Seed.h(token))
    assert r.status_code == 200, r.text
    return r.json()["messages"]


@pytest.fixture
def cli(api, monkeypatch):
    from typer.testing import CliRunner

    from grid import cli as grid_cli

    grid_cli.set_client_factory(lambda: api)
    runner = CliRunner()

    def run(*args, token=None):
        env = {"GRID_TOKEN": token} if token else {"GRID_TOKEN": ""}
        return runner.invoke(grid_cli.app, list(args), env=env)

    yield run
    grid_cli.set_client_factory(None)
