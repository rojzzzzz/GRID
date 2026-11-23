import pytest

from grid.core import SCHEMA, Database, GridError, normalize_path, parse_scope, paths_overlap
from conftest import assign, mk


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


# --- Task 2: tasks, scopes, settings -----------------------------------------

def test_only_lead_or_human_manage_tasks(api, seed):                  # spec 4
    for tok in (seed.frontend, seed.reviewer):
        assert api.post("/tasks", json={"title": "x"}, headers=seed.h(tok)).status_code == 403
    t = mk(api, seed, "A", ["web/"])
    for tok in (seed.frontend, seed.reviewer):
        for path, body in (("assign", {"owner": "frontend"}), ("depends", {"on": "T-1"}), ("reviewer", {"reviewer": "checker"})):
            r = api.post(f"/tasks/{t['id']}/{path}", json=body, headers=seed.h(tok))
            assert r.status_code == 403 and "lead or a human" in r.json()["message"]


def test_owner_must_be_worker_and_reviewer_rules(api, seed):          # spec 5
    t = mk(api, seed, "A", ["web/"])
    for who in ("checker", "planner"):
        assert assign(api, seed, t, who).status_code == 422
    assert assign(api, seed, t, "frontend").json()["owner"] == "frontend"
    assert api.post(f"/tasks/{t['id']}/reviewer", json={"reviewer": "frontend"}, headers=seed.h(seed.lead)).status_code == 422
    assert api.post(f"/tasks/{t['id']}/reviewer", json={"reviewer": "checker"}, headers=seed.h(seed.lead)).json()["reviewer"] == "checker"


def test_dependency_cycles_rejected(api, seed):                       # Review Focus 3
    a, b = mk(api, seed, "A", ["a/"]), mk(api, seed, "B", ["b/"])
    dep = lambda x, y: api.post(f"/tasks/{x['id']}/depends", json={"on": y["id"]}, headers=seed.h(seed.lead))
    assert dep(a, a).json()["error"] == "dependency_cycle"
    dep(b, a)
    assert dep(a, b).json()["error"] == "dependency_cycle"


def test_assign_blocked_when_dependency_open(api, seed):
    a, b = mk(api, seed, "A", ["a/"]), mk(api, seed, "B", ["b/"])
    api.post(f"/tasks/{b['id']}/depends", json={"on": a["id"]}, headers=seed.h(seed.lead))
    assert assign(api, seed, b, "backend").json()["status"] == "blocked"


def test_status_override_humans_only(api, seed):
    t = mk(api, seed, "A", ["a/"])
    assert api.post(f"/tasks/{t['id']}/status", json={"status": "done"}, headers=seed.h(seed.lead)).status_code == 403
    assert api.post(f"/tasks/{t['id']}/status", json={"status": "done"}, headers=seed.h(seed.human)).json()["status"] == "done"


def test_settings(api, seed):
    assert api.get("/project/settings", headers=seed.h(seed.frontend)).json()["message_cap"] == 2000
    patch = lambda tok, k, v: api.patch("/project/settings", json={"key": k, "value": v}, headers=seed.h(tok))
    assert patch(seed.frontend, "message_cap", 500).status_code == 403
    assert patch(seed.lead, "message_cap", 500).json()["message_cap"] == 500
    assert patch(seed.lead, "nope", 1).json()["error"] == "bad_setting"


def test_mutations_logged(api, seed):
    t = mk(api, seed, "A", ["a/"]); assign(api, seed, t, "frontend")
    acts = [e["action"] for e in api.get(f"/activity?task={t['id']}", headers=seed.h(seed.lead)).json()["activity"]]
    assert acts == ["task_created", "task_assigned"]


def test_scope_normalization_and_prefix_edges():                      # Review Focus 2
    assert normalize_path("./api\\auth\\") == normalize_path("api/auth/") == normalize_path(" api//auth ") == "api/auth"
    for bad in ("", "  ", "../x", "a/../b", "/etc", "C:/x"):
        with pytest.raises(GridError): normalize_path(bad)
    assert paths_overlap("api/auth", "api/auth/session.py") and paths_overlap("api/auth/session.py", "api/auth")
    assert not paths_overlap("api/auth", "api/authz")
    assert parse_scope(["web/ api/a.py, api/a.py"]) == ["web", "api/a.py"]


def test_assign_without_scope_rejected(api, seed):                     # spec 6
    t = api.post("/tasks", json={"title": "x"}, headers=seed.h(seed.lead)).json()
    r = assign(api, seed, t, "frontend")
    assert r.json()["error"] == "no_scope" and "grid task scope" in r.json()["fix"]


def test_overlap_rejected_then_allowed(api, seed):                     # spec 7
    t1 = mk(api, seed, "A", ["api/auth/"]); assign(api, seed, t1, "frontend")
    t2 = mk(api, seed, "B", ["api/auth/session.py"])
    r = assign(api, seed, t2, "backend")
    assert r.status_code == 409 and t1["id"] in r.json()["message"] and "--allow-overlap" in r.json()["fix"]
    assert assign(api, seed, t2, "backend", allow_overlap=True).status_code == 200
    acts = [e["action"] for e in api.get(f"/activity?task={t2['id']}", headers=seed.h(seed.lead)).json()["activity"]]
    assert "overlap_allowed" in acts
    assert assign(api, seed, mk(api, seed, "C", ["web/"]), "backend").status_code == 200


def test_overlap_cleared_when_done(api, seed):                         # spec 8
    t1 = mk(api, seed, "A", ["api/auth/"]); assign(api, seed, t1, "frontend")
    api.post(f"/tasks/{t1['id']}/status", json={"status": "done"}, headers=seed.h(seed.human))
    assert assign(api, seed, mk(api, seed, "B", ["api/auth/session.py"]), "backend").status_code == 200


def test_rescoping_active_task_is_checked(api, seed):
    t1 = mk(api, seed, "A", ["api/"]); assign(api, seed, t1, "frontend")
    t2 = mk(api, seed, "B", ["web/"]); assign(api, seed, t2, "backend")
    assert api.put(f"/tasks/{t2['id']}/scope", json={"scope": ["api/x.py"]}, headers=seed.h(seed.lead)).status_code == 409
