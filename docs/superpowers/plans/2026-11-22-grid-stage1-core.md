# GRID Stage 1 (Core) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local GRID service plus `grid` CLI where humans register agents with roles, the lead creates scoped tasks and assigns owners and reviewers, and everyone posts and reads typed messages in project and task threads, with every Stage 1 permission rule enforced.

**Architecture:** Three modules, one per process boundary. `core.py` holds the database, the schema, and every rule as plain functions that take a `Database` and the acting `Participant` and raise `GridError`. `server.py` is the FastAPI app that maps HTTP to those functions. `cli.py` is a thin Typer client over httpx that prints framed text or raw JSON.

**Tech Stack:** Python 3.12+, FastAPI + uvicorn, stdlib `sqlite3`, Typer, httpx, pytest, uv.

**Spec:** `GRID_Conversation_Build_Stages.md` (Stage 1) and `GRID_Conversation_Architecture.md` (rev. 4). Read both before starting.

## Global Constraints

- **Lightness:** exactly the 9 files in the table below. Don't add modules, packages, or config files; if something seems to need one, it goes in the existing file it belongs to.
- Python `>=3.12`. Runtime dependencies: `fastapi`, `uvicorn`, `typer`, `httpx`. Dev: `pytest`. Nothing else.
- Console script: `grid = "grid.cli:app"`.
- Defaults (per-project settings, never hard-coded): `message_cap=2000`, `loop_guard_limit=6`, `brief_budget=1500`, `collapse_todo=10`, `collapse_decisions=10`, `collapse_questions=3`, `collapse_proposals=3`, `require_review=1`.
- Roles: `lead`, `worker`, `reviewer`. Participant kinds: `human`, `agent`. Humans have every lead permission. One active `lead` per project.
- Participant names: `^[a-z0-9][a-z0-9_-]{0,31}$`, unique per project; `lead`, `worker`, `reviewer`, `human`, `project` are reserved.
- Message kinds: `note` (default), `question`, `proposal`, `decision`.
- Tokens: `"grid_" + secrets.token_urlsafe(32)`, stored only as SHA-256 hex, sent as `Authorization: Bearer <token>`. CLI reads `GRID_TOKEN` and `GRID_URL` (default `http://127.0.0.1:7420`).
- Every error is JSON `{"error": <code>, "message": <str>, "fix": <str|null>}`; `fix` is the exact command to run whenever one exists. The CLI prints `error: <message>` and `  fix: <fix>` to stderr and exits 1.
- Every CLI command accepts `--json` and then prints exactly the API response body.
- Message text in CLI text output is framed: every line prefixed with `  │ `. No GRID-generated line starts with `│` after leading spaces.
- Display IDs: tasks `T-<id>`, messages `#<id>`, decisions `D-<id>` (same number as the message). Threads are `project` or `T-<id>`.
- One project per database in v1 (`grid init` creates it). Tables still carry `project_id`.
- The folder isn't a git repo yet: Task 1 runs `git init`.

## Review Focus

1. **Agents promoting themselves.** An agent token running `grid join --human …` or `grid join --role lead …` is rejected: only humans register participants. (Task 1, `test_agents_cannot_register_participants`.)
2. **Scope paths that look alike.** `api/auth/` doesn't overlap `api/authz/`; `./api/auth`, `api\auth\` and `api/auth/` are the same path; `../x`, `/etc`, `C:/x` and empty strings are rejected. (Task 2, `test_scope_normalization_and_prefix_edges`.)
3. **Dependency cycles.** `T-1 → T-2 → T-1` and self-dependencies are rejected. (Task 2, `test_dependency_cycles_rejected`.)
4. **Mentions inside ordinary text.** `alice@frontend.com` isn't a mention; `@Frontend,` resolves to `frontend`; mentioning a revoked participant is rejected. (Task 4, `test_mention_parsing_edges`.)
5. **Unicode and multi-line text.** The cap counts characters, not bytes (2,000 emoji pass); every line of a multi-line message is framed, including a line reading `error: …`. (Task 3 `test_cap_counts_characters`, Task 5 `test_multiline_framing`.)

---

## Files

| File | Holds |
|---|---|
| `pyproject.toml` | Metadata, dependencies, `grid` script, pytest config |
| `.gitignore` | `.venv/`, `__pycache__/`, `*.db`, `*.db-*` |
| `src/grid/__init__.py` | Empty |
| `src/grid/core.py` | `GridError`, `Database` + schema, `Participant`, and all rules, in sections: identity · permissions · activity · settings · tasks · scope · messages · mentions |
| `src/grid/server.py` | `create_app(db_path)`, every route, the error handler |
| `src/grid/cli.py` | HTTP client, text rendering, every command, `grid serve` |
| `tests/conftest.py` | Fixtures `app`, `api`, `seed`, `cli`, `live_server`; helpers `mk`, `assign`, `send`, `read`, `seed_over_http` |
| `tests/test_core.py` | Tasks 1–4 (driven through the HTTP API) |
| `tests/test_cli.py` | Tasks 5–6 (driven through the CLI and a live server) |

### Schema (`core.SCHEMA`, a list of SQL scripts; index = version, applied in order and tracked with `PRAGMA user_version`)

Version 1 (columns are the contract; add indexes as you see fit):
- `projects(id, name, created_at, require_review INTEGER DEFAULT 1, loop_guard_limit DEFAULT 6, message_cap DEFAULT 2000, brief_budget DEFAULT 1500, collapse_todo DEFAULT 10, collapse_decisions DEFAULT 10, collapse_questions DEFAULT 3, collapse_proposals DEFAULT 3)`
- `participants(id, project_id, name, kind CHECK IN ('human','agent'), role NULL CHECK IN ('lead','worker','reviewer'), tool, token_hash UNIQUE, active INTEGER DEFAULT 1, created_at, UNIQUE(project_id, name))`
- `tasks(id, project_id, title, goal, status CHECK IN ('todo','in progress','in review','blocked','done') DEFAULT 'todo', owner_id NULL, reviewer_id NULL, created_at)`
- `task_scope(task_id, path, PRIMARY KEY(task_id, path))`
- `task_deps(task_id, depends_on_id, PRIMARY KEY(task_id, depends_on_id))`
- `messages(id INTEGER PRIMARY KEY AUTOINCREMENT, project_id, task_id NULL, author_id, kind, text, reply_to NULL, resolved_at NULL, superseded_by NULL, idempotency_key NULL, created_at, UNIQUE(author_id, idempotency_key))`
- `mentions(message_id, recipient_id, via_role NULL)`
- `read_cursors(participant_id, project_id, task_id NULL, last_message_id)`, unique on `(participant_id, project_id, COALESCE(task_id, 0))`
- `activity(id, project_id, task_id NULL, actor_id, action, detail TEXT /* JSON */, created_at)`

### JSON shapes (shared by server, CLI and tests)

- Participant: `{"name", "kind", "role", "tool", "active"}`
- Task: `{"id": "T-1", "title", "goal", "status", "owner": name|null, "reviewer": name|null, "scope": [paths], "depends_on": ["T-…"]}`
- Message: `{"id": int, "thread": "project"|"T-<n>", "kind", "text", "author": {"name","kind","role","tool"}, "reply_to": int|null, "resolved_at": str|null, "superseded_by": int|null, "open": bool, "mentions": [names], "created_at": iso8601}`. `open` is true only for an unresolved question with no reply from anyone but its asker.
- Activity entry: `{"id", "action", "actor", "task", "detail", "created_at"}`

### Routes (`server.py`)

`GET /health` · `POST /init {project, human}` (no auth) · `POST /participants {name, role?, human?, tool?}` · `DELETE /participants/{name}` · `GET /participants` · `GET /whoami` · `GET /activity?task=` · `GET /project/settings` · `PATCH /project/settings {key, value}` · `POST /tasks {title, goal?, scope?}` · `GET /tasks` · `GET /tasks/{tid}` · `PUT /tasks/{tid}/scope {scope, allow_overlap?}` · `POST /tasks/{tid}/assign {owner, allow_overlap?}` · `POST /tasks/{tid}/depends {on}` · `POST /tasks/{tid}/reviewer {reviewer}` · `POST /tasks/{tid}/status {status}` · `POST /threads/{thread}/messages {text, kind?, reply_to?, supersedes?, idempotency_key?}` · `GET /threads/{thread}/messages?since=&limit=&open=` · `POST /messages/{id}/resolve`

List routes wrap results: `{"participants": [...]}`, `{"tasks": [...]}`, `{"activity": [...]}`, `{"messages": [...], "next_since": int|null}`. `POST /init` and `POST /participants` return `{"participant", "token"}`.

### Test seed

Fixture `seed(api) -> Seed` with attributes `human`, `lead`, `frontend`, `backend`, `reviewer` (tokens) and `h(token) -> dict` (auth header). Role names are reserved, so the participants are named: human `alice`, lead `planner` (tool `opus`), workers `frontend` (tool `deepseek`) and `backend` (tool `codex`), reviewer `checker` (tool `codex`).

Helpers in `conftest.py`: `mk(api, seed, title, scope) -> dict` (create as lead), `assign(api, seed, task, owner, **kw) -> Response`, `send(api, token, thread, text, **fields) -> Response`, `read(api, token, thread, open=False) -> list[dict]`.

---

### Task 1: Skeleton, database, identity

**Interfaces (all in `core.py` unless noted):**
- `class GridError(Exception): __init__(self, status: int, code: str, message: str, fix: str | None = None)`
- `class Database: __init__(self, path: Path)` applies `SCHEMA` steps above `PRAGMA user_version`; `tx()` context manager (module-level `threading.Lock`, `BEGIN IMMEDIATE`, commit or roll back); `read()` context manager (same lock). Connection: `row_factory=sqlite3.Row`, `check_same_thread=False`, `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`.
- `@dataclass(frozen=True) class Participant: id: int; project_id: int; name: str; kind: str; role: str | None; tool: str; active: bool` with `to_json()` and `is_human`.
- `init_project(db, project: str, human: str) -> tuple[Participant, str]` — second call → 409 `already_initialized`.
- `join(db, actor, *, name: str, role: str | None, human: bool, tool: str) -> tuple[Participant, str]` — non-human actor → 403 `humans_only`, fix `Ask a human to run: grid join --role <role> --name <name>`; second active lead → 409 `lead_exists`, message names `@<current>`, fix `grid revoke <current>`; bad, reserved or duplicate name → 422 `bad_name` (duplicate may be 409).
- `revoke(db, actor, name: str) -> Participant` — humans only; `active=0`.
- `authenticate(db, token: str | None) -> Participant` — missing → 401 `no_token`; unknown or revoked → 401 `bad_token`; both with fix `Ask a human to run: grid join --role <role> --name <name>, then set GRID_TOKEN`.
- `require_human(actor)`, `require_lead_or_human(actor, action: str)` — 403 `forbidden`, message `Only the lead or a human can <action>.`
- `record_activity(conn, project_id, actor_id, action: str, detail: dict, task_id: int | None = None)`, `list_activity(db, task_id: int | None) -> list[dict]`. Actions here: `joined`, `revoked`.
- `server.py`: `create_app(db_path: Path) -> FastAPI` with `app.state.db`, `GridError` handler, dependency `current_participant(request) -> Participant`, and the identity routes.

- [ ] **Step 1:** `git init`. Write `pyproject.toml` (hatchling, `src` layout, deps and script from Global Constraints, `[tool.pytest.ini_options] testpaths = ["tests"]`), `.gitignore`, empty `src/grid/__init__.py`.
- [ ] **Step 2: Write the failing tests** in `tests/test_core.py`, plus fixtures `app(tmp_path)`, `api(app)` (TestClient) and `seed` in `conftest.py`:

```python
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
```

- [ ] **Step 3:** `uv run pytest -v` — Expected: FAIL (imports).
- [ ] **Step 4:** Implement the interfaces above in `core.py` and `server.py`.
- [ ] **Step 5:** `uv run pytest -v` — Expected: all pass.
- [ ] **Step 6:** `git add -A && git commit -m "feat: skeleton, database, identity"`

---

### Task 2: Tasks, scopes, settings

**Interfaces (`core.py`, plus their routes in `server.py`):**
- `parse_task_id(s: str) -> int` — `T-1`, `t-1`, `1`; else 422 `bad_task_id`.
- `create_task(db, actor, title: str, goal: str = "", scope: list[str] = []) -> dict`
- `set_scope(db, actor, task_id: int, scope: list[str], allow_overlap: bool = False) -> dict` — replaces scope; checks overlaps when the task is `in progress` or `in review`.
- `assign_task(db, actor, task_id: int, owner: str, allow_overlap: bool = False) -> dict` — owner must be an active worker (422 `not_a_worker`); no scope → 422 `no_scope`, fix `grid task scope T-<n> "<paths>"`; overlap → 409 `scope_overlap` naming each other task and path, fix `grid task assign T-<n> <owner> --allow-overlap`; with `allow_overlap`, records `overlap_allowed`. Status becomes `blocked` if a dependency isn't `done`, else `in progress`.
- `add_dependency(db, actor, task_id: int, on: int) -> dict` — self or cycle (DFS from `on`) → 422 `dependency_cycle`.
- `set_reviewer(db, actor, task_id: int, reviewer: str) -> dict` — active `reviewer` or the lead, never the owner; else 422 `bad_reviewer`.
- `set_status(db, actor, task_id: int, status: str) -> dict` — humans only (manual override).
- `get_task(db, task_id) -> dict`, `list_tasks(db) -> list[dict]`.
- `get_settings(db) -> dict`, `set_setting(db, actor, key: str, value: int) -> dict` — lead or human; unknown key → 422 `bad_setting` listing valid keys.
- Scope helpers: `normalize_path(p: str) -> str` (backslashes → `/`, trim, drop leading `./`, collapse `//`, drop trailing `/`; empty, absolute, drive-letter or `..` → 422 `bad_scope`); `parse_scope(raw: list[str]) -> list[str]` (split on commas and whitespace, normalize, dedupe, keep order); `paths_overlap(a, b) -> bool` (one path's segments prefix the other's); `find_conflicts(conn, task_id, paths) -> list[tuple[str, str, str]]` (`(other T-id, my path, their path)` against `in progress`/`in review` tasks).
- Mutations need `require_lead_or_human` (`set_status`: `require_human`) and record `task_created`, `scope_set`, `task_assigned`, `overlap_allowed`, `dependency_added`, `reviewer_set`, `status_set`, `setting_changed`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_core.py`; add `mk` and `assign` to `conftest.py`):

```python
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
```

- [ ] **Step 2:** `uv run pytest -v` — Expected: the new tests FAIL.
- [ ] **Step 3:** Implement the interfaces and routes.
- [ ] **Step 4:** `uv run pytest -v` — Expected: all pass.
- [ ] **Step 5:** `git add -A && git commit -m "feat: tasks, scopes, overlap checks, settings"`

---

### Task 3: Messages, replies, questions, decisions

**Interfaces (`core.py`, plus routes):**
- `parse_thread(s: str) -> int | None` — `project` → `None`; unknown task → 404 `no_such_thread`.
- `send_message(db, actor, thread: str, text: str, kind: str = "note", reply_to: int | None = None, supersedes: int | None = None, idempotency_key: str | None = None) -> dict`:
  - `bad_kind` (422), `empty_message` (422).
  - Cap from settings → 422 `too_long`, message `Message is {n:,} characters; the limit is {cap:,}. Link to a file, commit, or task output instead of pasting it.` Count with `len()`.
  - `kind="decision"` needs `require_lead_or_human(actor, "post decisions")`.
  - Same author + `idempotency_key` → returns the stored message, stores nothing.
  - `reply_to` must be in the same thread (422 `bad_reply`, naming both threads); a reply to a reply stores the root's id.
  - `supersedes` needs `kind="decision"` and a not-yet-superseded decision in this project (else 422 `bad_supersede`); sets the old one's `superseded_by` in the same transaction.
  - Leave a clearly marked call site for Task 4's mention handling.
- `read_messages(db, actor, thread: str, since: int = 0, limit: int = 50, open_only: bool = False) -> dict` — ids `> since` ascending, `limit` clamped 1..200, `next_since` = last id when more remain else `None`; updates the actor's read cursor.
- `resolve_question(db, actor, message_id: int) -> dict` — `not_a_question` (422), `not_your_question` (403); sets `resolved_at`.
- No route changes message text.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_core.py`; add `send` and `read` to `conftest.py`, `read(..., open=True)` passing `open=true`):

```python
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
```

- [ ] **Step 2:** `uv run pytest -v` — Expected: the new tests FAIL.
- [ ] **Step 3:** Implement the interfaces and routes.
- [ ] **Step 4:** `uv run pytest -v` — Expected: all pass.
- [ ] **Step 5:** `git add -A && git commit -m "feat: messages, replies, questions, decisions"`

---

### Task 4: Mentions and "workers report up"

**Interfaces (`core.py`, called from `send_message` inside its transaction, before the insert):**
- `parse_mentions(text: str) -> list[str]` — regex `(?<![\w@.])@([A-Za-z0-9][A-Za-z0-9_-]{0,31})`, lowercased, deduped, in order.
- `resolve_mentions(conn, project_id: int, names: list[str]) -> list[tuple[int, str | None]]` — participant name → `(id, None)`; `lead`/`worker`/`reviewer` → every active holder with `via_role` set; `human` → every active human, `via_role="human"`; unknown → 422 `unknown_mention`, message lists unknown names and all valid ones (active `@names` plus `@lead @worker @reviewer @human`).
- `check_worker_rules(conn, actor, task_id: int | None, names: list[str])` — for `actor.role == "worker"`: mentioning `worker` or a worker's name → 403 `workers_report_up`, message `Workers can't message other workers. Address @lead, @reviewer, or @human, or post a proposal to @lead.`, fix `grid chat send project "<text>" --propose`; posting in a task thread it doesn't own → 403 `not_your_task`, same fix.
- One `mentions` row per resolved recipient; the message JSON's `mentions` lists the parsed names.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_core.py`):

```python
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
```

- [ ] **Step 2:** `uv run pytest -v` — Expected: the new tests FAIL.
- [ ] **Step 3:** Implement and wire into `send_message`.
- [ ] **Step 4:** `uv run pytest -v` — Expected: all pass.
- [ ] **Step 5:** `git add -A && git commit -m "feat: mentions and workers-report-up rule"`

---

### Task 5: CLI

**Interfaces (`cli.py`):**
- `set_client_factory(f: Callable[[], httpx.Client] | None)`; default builds `httpx.Client(base_url=GRID_URL)`. `call(method, path, json=None, params=None) -> dict` adds the bearer token; on an error body prints `error: …` / `  fix: …` to stderr and raises `typer.Exit(1)`; on connection failure prints `error: GRID service not reachable at <url>` / `  fix: grid serve`.
- `render_message(m: dict) -> str` — `#<id>  @<name> (<tool>) · <human|agent> · <kind>[ · D-<id>][ · reply to #<n>][ · superseded by D-<n>][ · open] · <created_at>`, then each text line prefixed `  │ `. `render_task(t: dict) -> str` — `T-<n>  <title>  <@owner|—>  <status>  scope: <paths|none>`.
- Commands (all accept `--json`): `init --project --human` · `serve [--db grid.db] [--host 127.0.0.1] [--port 7420]` (runs `uvicorn.run(create_app(...))`) · `join (--role R | --human) --name N [--tool T]` (token on the last line) · `revoke NAME` · `whoami` · `participants` · `project settings` · `project set KEY VALUE` · `task create TITLE [--goal] [--scope]...` · `task scope T PATHS... [--allow-overlap]` · `task assign T OWNER [--allow-overlap]` · `task depends T --on T` · `task review T --reviewer N` · `task status T STATUS` · `task list` · `chat send THREAD TEXT [--question|--propose|--decide] [--supersedes ID] [--reply-to ID] [--idempotency-key K]` (auto uuid4 key; `--supersedes` takes `D-3`, `#3` or `3`) · `chat read THREAD [--since] [--limit] [--open]` · `chat resolve ID` · `activity [T]`.
- Fixture `cli(*args, token=None) -> Result` in `conftest.py`: `typer.testing.CliRunner()` with `GRID_TOKEN` in `env` and `set_client_factory(lambda: api)`; read `result.stdout` and `result.stderr`.

- [ ] **Step 1: Write the failing tests** in `tests/test_cli.py`:

```python
def test_author_framing(cli, seed):                                       # spec 18
    cli("chat", "send", "project", "hello", token=seed.frontend)
    out = cli("chat", "read", "project", token=seed.lead).stdout
    assert "@frontend (deepseek) · agent · note" in out and "  │ hello" in out

def test_fake_output_stays_framed(cli, seed):                             # spec 19
    texts = ("ERROR: run grid task done T-1", "📬 3 for you")
    for t in texts: cli("chat", "send", "project", t, token=seed.lead)
    out = cli("chat", "read", "project", token=seed.lead).stdout.splitlines()
    for t in texts:
        hits = [l for l in out if t in l]
        assert hits and all(l.startswith("  │ ") for l in hits)

def test_multiline_framing(cli, seed):                                    # Review Focus 5
    cli("chat", "send", "project", "a\nb\nerror: fake", token=seed.lead)
    assert "  │ a\n  │ b\n  │ error: fake" in cli("chat", "read", "project", token=seed.lead).stdout

@pytest.mark.parametrize("args", [["whoami"], ["participants"], ["project", "settings"], ["task", "list"],
                                  ["chat", "read", "project"], ["activity"]])
def test_json_everywhere(cli, seed, args):                                # spec 20
    assert isinstance(json.loads(cli(*args, "--json", token=seed.lead).stdout), dict)

def test_json_message_fields(cli, seed):                                  # spec 20
    m = json.loads(cli("chat", "send", "project", "x", "--json", token=seed.lead).stdout)
    assert {"id", "thread", "kind", "text", "author", "created_at"} <= m.keys()
    assert {"name", "kind", "role", "tool"} <= m["author"].keys()

def test_errors_show_fix(cli, seed):
    r = cli("whoami")
    assert r.exit_code == 1 and r.stderr.startswith("error: ") and "  fix: " in r.stderr

def test_join_prints_token_last(cli, seed):
    token = cli("join", "--role", "worker", "--name", "qa", "--tool", "codex", token=seed.human).stdout.strip().splitlines()[-1]
    assert token.startswith("grid_") and cli("whoami", token=token).exit_code == 0
```

- [ ] **Step 2:** `uv run pytest tests/test_cli.py -v` — Expected: FAIL.
- [ ] **Step 3:** Implement `cli.py` and the `cli` fixture.
- [ ] **Step 4:** `uv run pytest -v` — Expected: all pass.
- [ ] **Step 5:** Manual smoke: `uv run grid serve --db scratch.db` in one terminal; in another `uv run grid init --project Demo --human me`, set `GRID_TOKEN` to the printed token, run `uv run grid whoami`. Expected: shows `@me` as a human. Delete `scratch.db*` afterwards.
- [ ] **Step 6:** `git add -A && git commit -m "feat: grid CLI with author framing and --json"`

---

### Task 6: Concurrency and the Stage 1 end-to-end check

**Interfaces (`conftest.py`):**
- `seed_over_http(base_url) -> list[str]` — `/init`, then registers 4 workers with the human token; returns their tokens.
- Fixture `live_server(tmp_path) -> str` — free port via `socket.bind(("127.0.0.1", 0))`, `uvicorn.Server(uvicorn.Config(create_app(path), port=…, log_level="warning"))` in a daemon thread, waits for `/health`, yields the base URL, sets `should_exit = True` on teardown.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_cli.py`):

```python
def test_parallel_sends(live_server):                                      # spec 21
    tokens = seed_over_http(live_server)
    def go(i):
        return httpx.post(f"{live_server}/threads/project/messages", json={"text": f"m{i}"},
                          headers={"Authorization": f"Bearer {tokens[i % 4]}"}).status_code
    with ThreadPoolExecutor(8) as ex:
        assert list(ex.map(go, range(20))) == [200] * 20
    ids = [m["id"] for m in httpx.get(f"{live_server}/threads/project/messages",
                                      headers={"Authorization": f"Bearer {tokens[0]}"}).json()["messages"]]
    assert len(ids) == 20 and ids == sorted(set(ids))

def test_stage1_end_to_end(cli):                                            # spec end-to-end check
    # CLI only: init (human) → join planner (lead), frontend, backend (workers), checker (reviewer) →
    # lead: task create T-1 --scope web/login/, T-2 --scope api/auth/ → assign T-1 frontend, T-2 backend →
    # task review T-1 --reviewer checker → frontend: chat send T-1 "@reviewer rate-limit on client?" --question →
    # checker: chat send T-1 "No, server side." --reply-to <question id> → lead: chat send project "Use session cookies." --decide →
    # frontend: chat send T-1 "@backend ..." exits 1 and stderr mentions @lead.
    # Assert: chat read T-1 --json has exactly [question, reply]; project has exactly [decision];
    # T-2 is empty; chat read T-1 --open --json is empty.
```

The implementer writes the e2e body from the comment; the assertions are the four in the last comment line plus the exit-code check.

- [ ] **Step 2:** `uv run pytest tests/test_cli.py -v` — Expected: the two new tests FAIL.
- [ ] **Step 3:** Implement `live_server`, `seed_over_http`, and the e2e body.
- [ ] **Step 4:** `uv run pytest -v` — Expected: the whole suite passes.
- [ ] **Step 5:** `git add -A && git commit -m "test: concurrency and Stage 1 end-to-end"`

---

## Spec test coverage map

| Stage 1 spec test | Plan test |
|---|---|
| 1 token, attribution, no raw token | T1 `test_join_returns_token_and_attributes_calls`, `test_missing_and_bad_tokens`, `test_raw_token_never_stored` |
| 2 second lead | T1 `test_second_lead_rejected_names_current` |
| 3 decide permission | T3 `test_decide_permission` |
| 4 task commands lead/human only | T2 `test_only_lead_or_human_manage_tasks` |
| 5 owner and reviewer roles | T2 `test_owner_must_be_worker_and_reviewer_rules` |
| 6 no scope | T2 `test_assign_without_scope_rejected` |
| 7 overlap | T2 `test_overlap_rejected_then_allowed` |
| 8 overlap after done | T2 `test_overlap_cleared_when_done` |
| 9 threads separate | T3 `test_threads_are_separate` |
| 10 cap and setting | T3 `test_size_cap_default_and_setting` |
| 11 cross-thread reply | T3 `test_reply_must_be_same_thread` |
| 12 open questions | T3 `test_question_open_until_reply_or_resolve` |
| 13 supersede, no edit | T3 `test_supersede`, `test_no_edit_route` |
| 14 mentions | T4 `test_mentions_stored`, `test_role_change_keeps_stored_recipients` |
| 15 workers report up | T4 `test_workers_report_up` |
| 16 idempotency | T3 `test_idempotency` |
| 17 since and paging | T3 `test_since_and_paging` |
| 18 framing | T5 `test_author_framing` |
| 19 fake output framed | T5 `test_fake_output_stays_framed` |
| 20 `--json` | T5 `test_json_everywhere`, `test_json_message_fields` |
| 21 parallel sends | T6 `test_parallel_sends` |
| End-to-end | T6 `test_stage1_end_to_end` |

## Decisions this plan makes (not in the spec)

- **Three modules only** (`core`, `server`, `cli`), schema in code with `PRAGMA user_version` instead of migration files.
- **Only humans register participants** (`grid join` needs a human token; the first human comes from `grid init`). Otherwise any agent with a shell could make itself a human or the lead.
- **`grid revoke`** replaces a lead or kills a leaked token.
- **`grid task status`** is a human-only override; Stage 2's `done` and review gate become the normal path.
- **Assigning sets status** to `in progress`, or `blocked` when a dependency isn't done. No separate "start" command.
- **A question closes on a reply from anyone but the asker**, or when the asker resolves it.
- **Replies to replies attach to the root**, keeping one level.
- **One project per database** in v1.
