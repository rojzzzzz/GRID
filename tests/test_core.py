import pytest

from grid.core import SCHEMA, Database, GridError, normalize_path, parse_scope, paths_overlap, parse_mentions
from conftest import assign, mk, read, send


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


# --- Task 3: messages, replies, questions, decisions -------------------------

def test_threads_are_separate(api, seed):                              # spec 9
    t1 = mk(api, seed, "A", ["a/"])
    send(api, seed.lead, "project", "p"); send(api, seed.lead, t1["id"], "t")
    assert [m["text"] for m in read(api, seed.lead, "project")] == ["p"]
    assert [m["text"] for m in read(api, seed.lead, t1["id"])] == ["t"]


def test_size_cap_default_and_setting(api, seed):                      # spec 10
    assert send(api, seed.lead, "project", "x" * 2000).status_code == 200
    r = send(api, seed.lead, "project", "x" * 2001).json()
    assert r["error"] == "too_long" and "2,000" in r["message"] and "Link" in r["message"]
    api.patch("/project/settings", json={"key": "message_cap", "value": 500}, headers=seed.h(seed.lead))
    assert send(api, seed.lead, "project", "x" * 501).status_code == 422


def test_cap_counts_characters(api, seed):                             # Review Focus 5
    assert send(api, seed.lead, "project", "😀" * 2000).status_code == 200


def test_decide_permission(api, seed):                                  # spec 3
    for tok in (seed.frontend, seed.reviewer):
        assert send(api, tok, "project", "d", kind="decision").status_code == 403
    for tok in (seed.lead, seed.human):
        assert send(api, tok, "project", "d", kind="decision").status_code == 200


def test_idempotency(api, seed):                                        # spec 16
    a = send(api, seed.lead, "project", "x", idempotency_key="k1").json()
    b = send(api, seed.lead, "project", "x", idempotency_key="k1").json()
    assert a["id"] == b["id"] and len(read(api, seed.lead, "project")) == 1


def test_since_and_paging(api, seed):                                   # spec 17
    for i in range(120): send(api, seed.lead, "project", f"m{i}")
    seen, since = [], 0
    while True:
        page = api.get(f"/threads/project/messages?since={since}&limit=50", headers=seed.h(seed.lead)).json()
        seen += [m["text"] for m in page["messages"]]
        if page["next_since"] is None: break
        since = page["next_since"]
    assert seen == [f"m{i}" for i in range(120)]
    assert all(m["id"] > 100 for m in api.get("/threads/project/messages?since=100", headers=seed.h(seed.lead)).json()["messages"])


def test_unknown_thread_and_bad_kind(api, seed):
    assert send(api, seed.lead, "T-999", "x").json()["error"] == "no_such_thread"
    assert send(api, seed.lead, "project", "x", kind="order").json()["error"] == "bad_kind"
    assert send(api, seed.lead, "project", "").json()["error"] == "empty_message"


def test_reply_must_be_same_thread(api, seed):                          # spec 11
    t1 = mk(api, seed, "A", ["a/"])
    p = send(api, seed.lead, "project", "root").json()
    assert send(api, seed.lead, t1["id"], "x", reply_to=p["id"]).json()["error"] == "bad_reply"


def test_reply_to_reply_points_to_root(api, seed):
    root = send(api, seed.lead, "project", "root").json()
    r1 = send(api, seed.human, "project", "r1", reply_to=root["id"]).json()
    assert send(api, seed.lead, "project", "r2", reply_to=r1["id"]).json()["reply_to"] == root["id"]


def test_question_open_until_reply_or_resolve(api, seed):               # spec 12
    q1 = send(api, seed.lead, "project", "q1?", kind="question").json()
    q2 = send(api, seed.lead, "project", "q2?", kind="question").json()
    send(api, seed.lead, "project", "self-reply", reply_to=q1["id"])
    assert {m["id"] for m in read(api, seed.lead, "project", open=True)} == {q1["id"], q2["id"]}
    send(api, seed.human, "project", "answer", reply_to=q1["id"])
    assert api.post(f"/messages/{q2['id']}/resolve", headers=seed.h(seed.human)).json()["error"] == "not_your_question"
    api.post(f"/messages/{q2['id']}/resolve", headers=seed.h(seed.lead))
    assert read(api, seed.lead, "project", open=True) == []


def test_supersede(api, seed):                                           # spec 13
    d3 = send(api, seed.lead, "project", "cookies", kind="decision").json()
    d4 = send(api, seed.lead, "project", "jwt", kind="decision", supersedes=d3["id"]).json()
    assert {m["id"]: m for m in read(api, seed.lead, "project")}[d3["id"]]["superseded_by"] == d4["id"]
    n = send(api, seed.lead, "project", "note").json()
    for kind, target in (("decision", n["id"]), ("note", d4["id"]), ("decision", d3["id"])):
        assert send(api, seed.lead, "project", "x", kind=kind, supersedes=target).json()["error"] == "bad_supersede"


def test_no_edit_route(app):                                            # spec 13
    for route in app.routes:
        if "/messages" in getattr(route, "path", ""):
            assert not ({"PUT", "PATCH"} & set(getattr(route, "methods", set())))


# --- Task 4: mentions and "workers report up" --------------------------------

def mention_rows(app, mid):
    with app.state.db.read() as c:
        return {(r["name"], r["via_role"]) for r in c.execute(
            "select p.name, m.via_role from mentions m join participants p on p.id = m.recipient_id where m.message_id=?", (mid,))}


def test_mentions_stored(api, seed, app):                                 # spec 14
    m = send(api, seed.lead, "project", "@frontend @reviewer @human ping").json()
    assert mention_rows(app, m["id"]) == {("frontend", None), ("checker", "reviewer"), ("alice", "human")}
    r = send(api, seed.lead, "project", "@ghost hi").json()
    assert r["error"] == "unknown_mention" and "@frontend" in r["message"]


def test_role_change_keeps_stored_recipients(api, seed, app):             # spec 14
    m = send(api, seed.human, "project", "@lead hi").json()
    api.delete("/participants/planner", headers=seed.h(seed.human))
    api.post("/participants", json={"name": "lead2", "role": "lead"}, headers=seed.h(seed.human))
    assert mention_rows(app, m["id"]) == {("planner", "lead")}


def test_mention_parsing_edges(api, seed):                                 # Review Focus 4
    assert parse_mentions("mail alice@frontend.com now") == []
    assert parse_mentions("hey @Frontend, and @frontend.") == ["frontend"]
    api.delete("/participants/backend", headers=seed.h(seed.human))
    assert send(api, seed.lead, "project", "@backend hi").json()["error"] == "unknown_mention"


def test_workers_report_up(api, seed):                                     # spec 15
    t1 = mk(api, seed, "A", ["a/"]); assign(api, seed, t1, "frontend")
    t2 = mk(api, seed, "B", ["b/"]); assign(api, seed, t2, "backend")
    for text in ("@backend look", "@worker anyone?"):
        r = send(api, seed.frontend, t1["id"], text).json()
        assert r["error"] == "workers_report_up" and "@lead" in r["message"]
    assert send(api, seed.frontend, t2["id"], "hi").json()["error"] == "not_your_task"
    for thread in (t1["id"], "project"):
        assert send(api, seed.frontend, thread, "@lead @reviewer @human q").status_code == 200
    assert send(api, seed.reviewer, t2["id"], "@backend fyi").status_code == 200


# --- Final review fixes ----------------------------------------------------------

def test_scope_dot_segments_root_globs_and_case():
    assert normalize_path("api/./auth") == "api/auth" and normalize_path("./") == "."
    assert paths_overlap(".", "api") and paths_overlap("api/x", ".")
    assert paths_overlap("API/Auth", "api/auth/x.py")
    for bad in ("api/*", "~/x", "a?b"):
        with pytest.raises(GridError): normalize_path(bad)


def test_blocked_task_keeps_its_scope(api, seed):
    t1 = mk(api, seed, "A", ["api/auth/"]); assign(api, seed, t1, "frontend")
    t0 = mk(api, seed, "Z", ["z/"])
    api.post(f"/tasks/{t1['id']}/depends", json={"on": t0["id"]}, headers=seed.h(seed.lead))
    assert api.get(f"/tasks/{t1['id']}", headers=seed.h(seed.lead)).json()["status"] == "blocked"
    assert assign(api, seed, mk(api, seed, "B", ["api/auth/session.py"]), "backend").status_code == 409


def test_open_filter_does_not_advance_read_cursor(api, seed, app):
    send(api, seed.lead, "project", "q?", kind="question")
    send(api, seed.lead, "project", "note")
    read(api, seed.backend, "project", open=True)
    with app.state.db.read() as c:
        assert c.execute("select count(*) from read_cursors").fetchone()[0] == 0


def test_last_human_cannot_be_revoked(api, seed):
    r = api.delete("/participants/alice", headers=seed.h(seed.human))
    assert r.status_code == 409 and r.json()["error"] == "last_human"


def test_safety_settings_humans_only_and_validated(api, seed):
    patch = lambda tok, k, v: api.patch("/project/settings", json={"key": k, "value": v}, headers=seed.h(tok))
    for key in ("require_review", "loop_guard_limit"):
        assert patch(seed.lead, key, 1).status_code == 403
        assert patch(seed.human, key, 1).status_code == 200
    assert patch(seed.human, "require_review", 7).json()["error"] == "bad_setting"
    assert patch(seed.lead, "message_cap", 0).json()["error"] == "bad_setting"


def test_names_titles_tools_reject_control_characters(api, seed):
    join = lambda body: api.post("/participants", json=body, headers=seed.h(seed.human))
    assert join({"name": "evil\n", "role": "worker"}).status_code == 422
    assert join({"name": "lead\n", "role": "worker"}).status_code == 422
    assert join({"name": "qa", "role": "worker", "tool": "x\nerror: y"}).status_code == 422
    assert api.post("/tasks", json={"title": "Fix\nerror: y"}, headers=seed.h(seed.lead)).status_code == 422


def test_mentions_ignore_code_and_bad_lengths():
    assert parse_mentions("use `@dataclass` here") == []
    assert parse_mentions("```\n@pytest.fixture\ndef f(): ...\n```\nthanks @frontend") == ["frontend"]
    assert parse_mentions("ping @frontend-") == ["frontend"]
    assert parse_mentions("@" + "a" * 40) == ["a" * 40]
