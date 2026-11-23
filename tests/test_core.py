import pytest

from grid.core import SCHEMA, Database, GridError


# --- Task 1: skeleton, database, identity -----------------------------------

def test_schema_created_and_idempotent(tmp_path):
    Database(tmp_path / "g.db"); db = Database(tmp_path / "g.db")
    with db.read() as c:
        names = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
        assert c.execute("pragma user_version").fetchone()[0] == len(SCHEMA)
    assert {"projects", "participants", "tasks", "task_scope", "task_deps", "messages",
            "mentions", "read_cursors", "activity"} <= names


def test_tx_rolls_back_on_error(tmp_path):
    db = Database(tmp_path / "g.db")
    with pytest.raises(RuntimeError):
        with db.tx() as c:
            c.execute("insert into projects(name, created_at) values ('x', 'now')"); raise RuntimeError
    with db.read() as c:
        assert c.execute("select count(*) from projects").fetchone()[0] == 0


def test_grid_error_becomes_json(api, app):
    @app.get("/boom")
    def boom(): raise GridError(409, "conflict", "Nope.", "grid do-this")
    assert api.get("/boom").json() == {"error": "conflict", "message": "Nope.", "fix": "grid do-this"}


def test_join_returns_token_and_attributes_calls(seed, api):          # spec 1
    r = api.get("/whoami", headers=seed.h(seed.frontend)).json()
    assert r["name"] == "frontend" and r["role"] == "worker"


def test_missing_and_bad_tokens(api, seed):                            # spec 1
    r = api.get("/whoami"); assert r.status_code == 401 and "grid join" in r.json()["fix"]
    assert api.get("/whoami", headers={"Authorization": "Bearer nope"}).json()["error"] == "bad_token"


def test_raw_token_never_stored(seed, app):                            # spec 1
    with app.state.db.read() as c:
        dump = "".join(str(tuple(r)) for r in c.execute("select * from participants"))
    assert seed.frontend not in dump


def test_second_lead_rejected_names_current(api, seed):                # spec 2
    r = api.post("/participants", json={"name": "lead2", "role": "lead"}, headers=seed.h(seed.human))
    assert r.status_code == 409 and "@planner" in r.json()["message"] and r.json()["fix"] == "grid revoke planner"


def test_agents_cannot_register_participants(api, seed):               # Review Focus 1
    for body in ({"name": "evil", "human": True}, {"name": "evil", "role": "lead"}, {"name": "w9", "role": "worker"}):
        assert api.post("/participants", json=body, headers=seed.h(seed.lead)).status_code == 403


def test_revoked_token_rejected(api, seed):
    api.delete("/participants/backend", headers=seed.h(seed.human))
    assert api.get("/whoami", headers=seed.h(seed.backend)).json()["error"] == "bad_token"


def test_bad_and_reserved_names(api, seed):
    for n in ("Lead", "lead", "human", "project", "a" * 33, "-x", "frontend"):
        assert api.post("/participants", json={"name": n, "role": "worker"}, headers=seed.h(seed.human)).status_code in (409, 422)


def test_init_only_once(api, seed):
    assert api.post("/init", json={"project": "x", "human": "bob"}).json()["error"] == "already_initialized"
