"""GRID core: database, schema, and every rule.

Sections: errors · database · participants & identity · permissions · activity
Every rule is a plain function taking a Database and the acting Participant.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


# --- errors -----------------------------------------------------------------

class GridError(Exception):
    """An error an agent can act on: a code, a message, and the command that fixes it."""

    def __init__(self, status: int, code: str, message: str, fix: str | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.fix = status, code, message, fix

    def to_json(self) -> dict:
        return {"error": self.code, "message": self.message, "fix": self.fix}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- database ---------------------------------------------------------------

# One entry per schema version, applied in order; PRAGMA user_version records progress.
SCHEMA = [
    """
    CREATE TABLE projects (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
        require_review INTEGER NOT NULL DEFAULT 1, loop_guard_limit INTEGER NOT NULL DEFAULT 6,
        message_cap INTEGER NOT NULL DEFAULT 2000, brief_budget INTEGER NOT NULL DEFAULT 1500,
        collapse_todo INTEGER NOT NULL DEFAULT 10, collapse_decisions INTEGER NOT NULL DEFAULT 10,
        collapse_questions INTEGER NOT NULL DEFAULT 3, collapse_proposals INTEGER NOT NULL DEFAULT 3);
    CREATE TABLE participants (
        id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
        name TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('human', 'agent')),
        role TEXT CHECK (role IN ('lead', 'worker', 'reviewer')), tool TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
        UNIQUE (project_id, name));
    CREATE TABLE tasks (
        id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
        title TEXT NOT NULL, goal TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'todo'
            CHECK (status IN ('todo', 'in progress', 'in review', 'blocked', 'done')),
        owner_id INTEGER REFERENCES participants(id), reviewer_id INTEGER REFERENCES participants(id),
        created_at TEXT NOT NULL);
    CREATE TABLE task_scope (
        task_id INTEGER NOT NULL REFERENCES tasks(id), path TEXT NOT NULL, PRIMARY KEY (task_id, path));
    CREATE TABLE task_deps (
        task_id INTEGER NOT NULL REFERENCES tasks(id), depends_on_id INTEGER NOT NULL REFERENCES tasks(id),
        PRIMARY KEY (task_id, depends_on_id));
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL REFERENCES projects(id),
        task_id INTEGER REFERENCES tasks(id), author_id INTEGER NOT NULL REFERENCES participants(id),
        kind TEXT NOT NULL, text TEXT NOT NULL, reply_to INTEGER REFERENCES messages(id),
        resolved_at TEXT, superseded_by INTEGER REFERENCES messages(id), idempotency_key TEXT,
        created_at TEXT NOT NULL, UNIQUE (author_id, idempotency_key));
    CREATE INDEX messages_thread ON messages (project_id, task_id, id);
    CREATE TABLE mentions (
        message_id INTEGER NOT NULL REFERENCES messages(id),
        recipient_id INTEGER NOT NULL REFERENCES participants(id), via_role TEXT);
    CREATE TABLE read_cursors (
        participant_id INTEGER NOT NULL REFERENCES participants(id), project_id INTEGER NOT NULL,
        task_id INTEGER, last_message_id INTEGER NOT NULL);
    CREATE UNIQUE INDEX read_cursors_thread ON read_cursors (participant_id, project_id, COALESCE(task_id, 0));
    CREATE TABLE activity (
        id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, task_id INTEGER,
        actor_id INTEGER NOT NULL REFERENCES participants(id), action TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
    """,
]

_lock = threading.Lock()


class Database:
    """One SQLite file, one connection, one lock: GRID is the only writer."""

    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        for pragma in ("journal_mode=WAL", "foreign_keys=ON", "busy_timeout=5000"):
            self.conn.execute(f"PRAGMA {pragma}")
        with _lock:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            for i, script in enumerate(SCHEMA[version:], start=version + 1):
                self.conn.executescript(f"BEGIN; {script}; PRAGMA user_version = {i}; COMMIT;")

    @contextmanager
    def tx(self):
        with _lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    @contextmanager
    def read(self):
        with _lock:
            yield self.conn


# --- participants & identity ------------------------------------------------

ROLES = ("lead", "worker", "reviewer")
RESERVED = {"lead", "worker", "reviewer", "human", "project"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
JOIN_FIX = "Ask a human to run: grid join --role <role> --name <name>"


@dataclass(frozen=True)
class Participant:
    id: int
    project_id: int
    name: str
    kind: str
    role: str | None
    tool: str
    active: bool

    @property
    def is_human(self) -> bool:
        return self.kind == "human"

    def to_json(self) -> dict:
        return {"name": self.name, "kind": self.kind, "role": self.role, "tool": self.tool,
                "active": self.active}

    @staticmethod
    def from_row(row) -> Participant:
        return Participant(row["id"], row["project_id"], row["name"], row["kind"], row["role"],
                           row["tool"], bool(row["active"]))


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_participant(conn, project_id: int, name: str, kind: str, role: str | None,
                     tool: str) -> tuple[Participant, str]:
    if not NAME_RE.match(name) or name in RESERVED:
        raise GridError(422, "bad_name",
                        f"'{name}' isn't a valid name. Use 1-32 lowercase letters, digits, '-' or '_', "
                        f"not one of: {', '.join(sorted(RESERVED))}.")
    if conn.execute("SELECT 1 FROM participants WHERE project_id=? AND name=?",
                    (project_id, name)).fetchone():
        raise GridError(409, "bad_name", f"@{name} already exists. Pick another name.")
    token = "grid_" + secrets.token_urlsafe(32)
    cur = conn.execute(
        "INSERT INTO participants (project_id, name, kind, role, tool, token_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", (project_id, name, kind, role, tool, _hash(token), now()))
    row = conn.execute("SELECT * FROM participants WHERE id=?", (cur.lastrowid,)).fetchone()
    return Participant.from_row(row), token


def init_project(db: Database, project: str, human: str) -> tuple[Participant, str]:
    with db.tx() as c:
        if c.execute("SELECT 1 FROM projects").fetchone():
            raise GridError(409, "already_initialized", "This GRID database already has a project.")
        pid = c.execute("INSERT INTO projects (name, created_at) VALUES (?, ?)",
                        (project, now())).lastrowid
        p, token = _new_participant(c, pid, human, "human", None, "human")
        record_activity(c, pid, p.id, "joined", {"name": p.name, "kind": "human"})
        return p, token


def join(db: Database, actor: Participant, *, name: str, role: str | None, human: bool,
         tool: str) -> tuple[Participant, str]:
    if not actor.is_human:
        raise GridError(403, "humans_only", "Only a human can register participants.", JOIN_FIX)
    if not human and role not in ROLES:
        raise GridError(422, "bad_role", f"Role must be one of: {', '.join(ROLES)}.")
    with db.tx() as c:
        if role == "lead":
            cur = c.execute("SELECT name FROM participants WHERE project_id=? AND role='lead' AND active=1",
                            (actor.project_id,)).fetchone()
            if cur:
                raise GridError(409, "lead_exists", f"@{cur['name']} is already the lead.",
                                f"grid revoke {cur['name']}")
        p, token = _new_participant(c, actor.project_id, name, "human" if human else "agent",
                                    None if human else role, "human" if human else (tool or "unknown"))
        record_activity(c, actor.project_id, actor.id, "joined", {"name": p.name, "role": p.role})
        return p, token


def revoke(db: Database, actor: Participant, name: str) -> Participant:
    require_human(actor)
    with db.tx() as c:
        row = c.execute("SELECT * FROM participants WHERE project_id=? AND name=? AND active=1",
                        (actor.project_id, name)).fetchone()
        if not row:
            raise GridError(404, "no_such_participant", f"No active participant named @{name}.",
                            "grid participants")
        c.execute("UPDATE participants SET active=0 WHERE id=?", (row["id"],))
        record_activity(c, actor.project_id, actor.id, "revoked", {"name": name})
        return Participant.from_row(c.execute("SELECT * FROM participants WHERE id=?", (row["id"],)).fetchone())


def authenticate(db: Database, token: str | None) -> Participant:
    fix = JOIN_FIX + ", then set GRID_TOKEN"
    if not token:
        raise GridError(401, "no_token", "No GRID token.", fix)
    with db.read() as c:
        row = c.execute("SELECT * FROM participants WHERE token_hash=? AND active=1",
                        (_hash(token),)).fetchone()
    if not row:
        raise GridError(401, "bad_token", "This GRID token is unknown or revoked.", fix)
    return Participant.from_row(row)


def list_participants(db: Database, actor: Participant) -> list[dict]:
    with db.read() as c:
        rows = c.execute("SELECT * FROM participants WHERE project_id=? ORDER BY id",
                         (actor.project_id,)).fetchall()
    return [Participant.from_row(r).to_json() for r in rows]


# --- permissions ------------------------------------------------------------

def require_human(actor: Participant) -> None:
    if not actor.is_human:
        raise GridError(403, "forbidden", "Only a human can do this.")


def require_lead_or_human(actor: Participant, action: str) -> None:
    if not (actor.is_human or actor.role == "lead"):
        raise GridError(403, "forbidden", f"Only the lead or a human can {action}.")


# --- activity ---------------------------------------------------------------

def record_activity(conn, project_id: int, actor_id: int, action: str, detail: dict,
                    task_id: int | None = None) -> None:
    conn.execute("INSERT INTO activity (project_id, task_id, actor_id, action, detail, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?)",
                 (project_id, task_id, actor_id, action, json.dumps(detail), now()))


def list_activity(db: Database, actor: Participant, task_id: int | None = None) -> list[dict]:
    sql = ("SELECT a.*, p.name AS actor FROM activity a JOIN participants p ON p.id = a.actor_id "
           "WHERE a.project_id=?")
    args: list = [actor.project_id]
    if task_id is not None:
        sql += " AND a.task_id=?"
        args.append(task_id)
    with db.read() as c:
        rows = c.execute(sql + " ORDER BY a.id", args).fetchall()
    return [{"id": r["id"], "action": r["action"], "actor": r["actor"],
             "task": f"T-{r['task_id']}" if r["task_id"] else None,
             "detail": json.loads(r["detail"]), "created_at": r["created_at"]} for r in rows]


# --- settings ---------------------------------------------------------------

SETTINGS = ("require_review", "loop_guard_limit", "message_cap", "brief_budget", "collapse_todo",
            "collapse_decisions", "collapse_questions", "collapse_proposals")


def _settings(conn, project_id: int) -> dict:
    row = conn.execute(f"SELECT {', '.join(SETTINGS)} FROM projects WHERE id=?", (project_id,)).fetchone()
    return dict(row)


def get_settings(db: Database, actor: Participant) -> dict:
    with db.read() as c:
        return _settings(c, actor.project_id)


def set_setting(db: Database, actor: Participant, key: str, value: int) -> dict:
    require_lead_or_human(actor, "change project settings")
    if key not in SETTINGS:
        raise GridError(422, "bad_setting", f"Unknown setting '{key}'. Valid: {', '.join(SETTINGS)}.")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise GridError(422, "bad_setting", f"'{key}' needs a whole number of 0 or more.")
    with db.tx() as c:
        c.execute(f"UPDATE projects SET {key}=? WHERE id=?", (value, actor.project_id))
        record_activity(c, actor.project_id, actor.id, "setting_changed", {"key": key, "value": value})
        return _settings(c, actor.project_id)


# --- scope ------------------------------------------------------------------

ACTIVE = ("in progress", "in review")


def normalize_path(p: str) -> str:
    raw = p
    p = p.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = re.sub(r"/+", "/", p).rstrip("/")
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p) or ".." in p.split("/"):
        raise GridError(422, "bad_scope",
                        f"'{raw}' isn't a usable scope path. Use a path relative to the repo root, "
                        "without '..', like 'api/auth/'.")
    return p


def parse_scope(raw: list[str]) -> list[str]:
    out: list[str] = []
    for item in raw:
        for part in re.split(r"[,\s]+", item.strip()):
            if part:
                p = normalize_path(part)
                if p not in out:
                    out.append(p)
    return out


def paths_overlap(a: str, b: str) -> bool:
    sa, sb = a.split("/"), b.split("/")
    n = min(len(sa), len(sb))
    return sa[:n] == sb[:n]


def find_conflicts(conn, task_id: int, paths: list[str]) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT s.task_id, s.path FROM task_scope s JOIN tasks t ON t.id = s.task_id "
        "WHERE t.status IN (?, ?) AND s.task_id != ? ORDER BY s.task_id", (*ACTIVE, task_id)).fetchall()
    return [(f"T-{r['task_id']}", mine, r["path"]) for mine in paths for r in rows
            if paths_overlap(mine, r["path"])]


def _describe(conflicts: list[tuple[str, str, str]]) -> str:
    return "; ".join(f"{other} ({mine} ↔ {theirs})" for other, mine, theirs in conflicts)


# --- tasks ------------------------------------------------------------------

STATUSES = ("todo", "in progress", "in review", "blocked", "done")


def parse_task_id(s: str | int) -> int:
    m = re.fullmatch(r"(?:[Tt]-)?(\d+)", str(s).strip())
    if not m:
        raise GridError(422, "bad_task_id", f"'{s}' isn't a task id. Use the form T-12.", "grid task list")
    return int(m.group(1))


def _task_row(conn, actor: Participant, task_id: int):
    row = conn.execute("SELECT * FROM tasks WHERE id=? AND project_id=?",
                       (task_id, actor.project_id)).fetchone()
    if not row:
        raise GridError(404, "no_such_task", f"There is no task T-{task_id}.", "grid task list")
    return row


def _name(conn, participant_id: int | None) -> str | None:
    if participant_id is None:
        return None
    return conn.execute("SELECT name FROM participants WHERE id=?", (participant_id,)).fetchone()["name"]


def _task_json(conn, row) -> dict:
    scope = [r["path"] for r in conn.execute(
        "SELECT path FROM task_scope WHERE task_id=? ORDER BY rowid", (row["id"],))]
    deps = [f"T-{r['depends_on_id']}" for r in conn.execute(
        "SELECT depends_on_id FROM task_deps WHERE task_id=? ORDER BY depends_on_id", (row["id"],))]
    return {"id": f"T-{row['id']}", "title": row["title"], "goal": row["goal"], "status": row["status"],
            "owner": _name(conn, row["owner_id"]), "reviewer": _name(conn, row["reviewer_id"]),
            "scope": scope, "depends_on": deps}


def _participant_by_name(conn, actor: Participant, name: str):
    return conn.execute("SELECT * FROM participants WHERE project_id=? AND name=? AND active=1",
                        (actor.project_id, name.lstrip("@").lower())).fetchone()


def _open_deps(conn, task_id: int) -> bool:
    return conn.execute("SELECT 1 FROM task_deps d JOIN tasks t ON t.id = d.depends_on_id "
                        "WHERE d.task_id=? AND t.status != 'done'", (task_id,)).fetchone() is not None


def _check_overlap(conn, actor: Participant, task_id: int, paths: list[str], allow: bool, fix: str) -> None:
    conflicts = find_conflicts(conn, task_id, paths)
    if conflicts and not allow:
        raise GridError(409, "scope_overlap",
                        f"T-{task_id} overlaps active work: {_describe(conflicts)}. "
                        "Make one task depend on the other, or allow the overlap.", fix)
    if conflicts:
        record_activity(conn, actor.project_id, actor.id, "overlap_allowed",
                        {"conflicts": [list(c) for c in conflicts]}, task_id)


def create_task(db: Database, actor: Participant, title: str, goal: str = "",
                scope: list[str] | None = None) -> dict:
    require_lead_or_human(actor, "create tasks")
    if not title.strip():
        raise GridError(422, "bad_title", "A task needs a title.")
    paths = parse_scope(scope or [])
    with db.tx() as c:
        tid = c.execute("INSERT INTO tasks (project_id, title, goal, created_at) VALUES (?, ?, ?, ?)",
                        (actor.project_id, title.strip(), goal, now())).lastrowid
        c.executemany("INSERT INTO task_scope (task_id, path) VALUES (?, ?)", [(tid, p) for p in paths])
        record_activity(c, actor.project_id, actor.id, "task_created", {"title": title, "scope": paths}, tid)
        return _task_json(c, _task_row(c, actor, tid))


def set_scope(db: Database, actor: Participant, task_id: int, scope: list[str],
              allow_overlap: bool = False) -> dict:
    require_lead_or_human(actor, "set task scopes")
    paths = parse_scope(scope)
    if not paths:
        raise GridError(422, "no_scope", "A scope needs at least one path.")
    with db.tx() as c:
        row = _task_row(c, actor, task_id)
        if row["status"] in ACTIVE:
            _check_overlap(c, actor, task_id, paths, allow_overlap,
                           f"grid task scope T-{task_id} {' '.join(paths)} --allow-overlap")
        c.execute("DELETE FROM task_scope WHERE task_id=?", (task_id,))
        c.executemany("INSERT INTO task_scope (task_id, path) VALUES (?, ?)", [(task_id, p) for p in paths])
        record_activity(c, actor.project_id, actor.id, "scope_set", {"scope": paths}, task_id)
        return _task_json(c, row)


def assign_task(db: Database, actor: Participant, task_id: int, owner: str,
                allow_overlap: bool = False) -> dict:
    require_lead_or_human(actor, "assign tasks")
    with db.tx() as c:
        _task_row(c, actor, task_id)
        who = _participant_by_name(c, actor, owner)
        if not who or who["role"] != "worker":
            raise GridError(422, "not_a_worker", f"@{owner} isn't an active worker; only workers own tasks.",
                            "grid participants")
        paths = [r["path"] for r in c.execute("SELECT path FROM task_scope WHERE task_id=?", (task_id,))]
        if not paths:
            raise GridError(422, "no_scope", f"T-{task_id} has no scope, so overlaps can't be checked.",
                            f'grid task scope T-{task_id} "<paths>"')
        _check_overlap(c, actor, task_id, paths, allow_overlap,
                       f"grid task assign T-{task_id} {who['name']} --allow-overlap")
        status = "blocked" if _open_deps(c, task_id) else "in progress"
        c.execute("UPDATE tasks SET owner_id=?, status=? WHERE id=?", (who["id"], status, task_id))
        record_activity(c, actor.project_id, actor.id, "task_assigned", {"owner": who["name"]}, task_id)
        return _task_json(c, _task_row(c, actor, task_id))


def add_dependency(db: Database, actor: Participant, task_id: int, on: int) -> dict:
    require_lead_or_human(actor, "add dependencies")
    with db.tx() as c:
        row = _task_row(c, actor, task_id)
        _task_row(c, actor, on)
        seen, stack = set(), [on]
        while stack:  # walk what `on` depends on; reaching task_id means a loop
            cur = stack.pop()
            if cur == task_id:
                raise GridError(422, "dependency_cycle",
                                f"T-{task_id} can't depend on T-{on}: that would make a loop.")
            if cur not in seen:
                seen.add(cur)
                stack += [r[0] for r in c.execute("SELECT depends_on_id FROM task_deps WHERE task_id=?", (cur,))]
        c.execute("INSERT OR IGNORE INTO task_deps (task_id, depends_on_id) VALUES (?, ?)", (task_id, on))
        if row["status"] == "in progress" and _open_deps(c, task_id):
            c.execute("UPDATE tasks SET status='blocked' WHERE id=?", (task_id,))
        record_activity(c, actor.project_id, actor.id, "dependency_added", {"on": f"T-{on}"}, task_id)
        return _task_json(c, _task_row(c, actor, task_id))


def set_reviewer(db: Database, actor: Participant, task_id: int, reviewer: str) -> dict:
    require_lead_or_human(actor, "assign reviewers")
    with db.tx() as c:
        row = _task_row(c, actor, task_id)
        who = _participant_by_name(c, actor, reviewer)
        if not who or who["role"] not in ("reviewer", "lead") or who["id"] == row["owner_id"]:
            raise GridError(422, "bad_reviewer",
                            f"@{reviewer} can't review T-{task_id}. A reviewer must be an active reviewer "
                            "or the lead, and never the task's owner.", "grid participants")
        c.execute("UPDATE tasks SET reviewer_id=? WHERE id=?", (who["id"], task_id))
        record_activity(c, actor.project_id, actor.id, "reviewer_set", {"reviewer": who["name"]}, task_id)
        return _task_json(c, _task_row(c, actor, task_id))


def set_status(db: Database, actor: Participant, task_id: int, status: str) -> dict:
    require_human(actor)
    if status not in STATUSES:
        raise GridError(422, "bad_status", f"Status must be one of: {', '.join(STATUSES)}.")
    with db.tx() as c:
        _task_row(c, actor, task_id)
        c.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
        record_activity(c, actor.project_id, actor.id, "status_set", {"status": status}, task_id)
        return _task_json(c, _task_row(c, actor, task_id))


def get_task(db: Database, actor: Participant, task_id: int) -> dict:
    with db.read() as c:
        return _task_json(c, _task_row(c, actor, task_id))


def list_tasks(db: Database, actor: Participant) -> list[dict]:
    with db.read() as c:
        rows = c.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY id", (actor.project_id,)).fetchall()
        return [_task_json(c, r) for r in rows]


# --- messages ---------------------------------------------------------------

KINDS = ("note", "question", "proposal", "decision")

# A question is open while unresolved and unanswered by anyone but its asker.
_OPEN_SQL = ("(m.kind = 'question' AND m.resolved_at IS NULL AND NOT EXISTS ("
             "SELECT 1 FROM messages r WHERE r.reply_to = m.id AND r.author_id != m.author_id))")


def _thread(conn, actor: Participant, thread: str) -> int | None:
    if thread == "project":
        return None
    try:
        return _task_row(conn, actor, parse_task_id(thread))["id"]
    except GridError:
        raise GridError(404, "no_such_thread", f"There is no thread '{thread}'. Use 'project' or a task id.",
                        "grid task list") from None


def _message_json(conn, row) -> dict:
    a = conn.execute("SELECT * FROM participants WHERE id=?", (row["author_id"],)).fetchone()
    is_open = conn.execute(f"SELECT {_OPEN_SQL} FROM messages m WHERE m.id=?", (row["id"],)).fetchone()[0]
    return {"id": row["id"], "thread": f"T-{row['task_id']}" if row["task_id"] else "project",
            "kind": row["kind"], "text": row["text"],
            "author": {"name": a["name"], "kind": a["kind"], "role": a["role"], "tool": a["tool"]},
            "reply_to": row["reply_to"], "resolved_at": row["resolved_at"],
            "superseded_by": row["superseded_by"], "open": bool(is_open),
            "mentions": _mention_names(row["text"]), "created_at": row["created_at"]}


def _mention_names(text: str) -> list[str]:
    return []  # filled in by the mentions section


def _message_row(conn, actor: Participant, message_id: int):
    row = conn.execute("SELECT * FROM messages WHERE id=? AND project_id=?",
                       (message_id, actor.project_id)).fetchone()
    if not row:
        raise GridError(404, "no_such_message", f"There is no message #{message_id}.")
    return row


def send_message(db: Database, actor: Participant, thread: str, text: str, kind: str = "note",
                 reply_to: int | None = None, supersedes: int | None = None,
                 idempotency_key: str | None = None) -> dict:
    if kind not in KINDS:
        raise GridError(422, "bad_kind", f"Message kind must be one of: {', '.join(KINDS)}.")
    if not text or not text.strip():
        raise GridError(422, "empty_message", "A message needs some text.")
    if kind == "decision":
        require_lead_or_human(actor, "post decisions")
    with db.tx() as c:
        task_id = _thread(c, actor, thread)
        cap = _settings(c, actor.project_id)["message_cap"]
        if len(text) > cap:
            raise GridError(422, "too_long", f"Message is {len(text):,} characters; the limit is {cap:,}. "
                            "Link to a file, commit, or task output instead of pasting it.")
        if idempotency_key:
            seen = c.execute("SELECT * FROM messages WHERE author_id=? AND idempotency_key=?",
                             (actor.id, idempotency_key)).fetchone()
            if seen:
                return _message_json(c, seen)
        if reply_to is not None:
            target = _message_row(c, actor, reply_to)
            if target["task_id"] != task_id:
                where = f"T-{target['task_id']}" if target["task_id"] else "project"
                raise GridError(422, "bad_reply", f"#{reply_to} is in the {where} thread, not {thread}. "
                                "Reply in the same thread.")
            reply_to = target["reply_to"] or target["id"]  # one level: attach to the root
        if supersedes is not None:
            old = c.execute("SELECT * FROM messages WHERE id=? AND project_id=?",
                            (supersedes, actor.project_id)).fetchone()
            if kind != "decision" or not old or old["kind"] != "decision" or old["superseded_by"]:
                raise GridError(422, "bad_supersede", "Only a new decision can replace a current decision "
                                f"(D-{supersedes} isn't one).", "grid chat read project")
        _check_mentions(c, actor, task_id, text)
        mid = c.execute(
            "INSERT INTO messages (project_id, task_id, author_id, kind, text, reply_to, idempotency_key, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (actor.project_id, task_id, actor.id, kind, text, reply_to, idempotency_key, now())).lastrowid
        _store_mentions(c, actor, mid, text)
        if supersedes is not None:
            c.execute("UPDATE messages SET superseded_by=? WHERE id=?", (mid, supersedes))
        return _message_json(c, _message_row(c, actor, mid))


def _check_mentions(conn, actor: Participant, task_id: int | None, text: str) -> None:
    pass  # filled in by the mentions section


def _store_mentions(conn, actor: Participant, message_id: int, text: str) -> None:
    pass  # filled in by the mentions section


def read_messages(db: Database, actor: Participant, thread: str, since: int = 0, limit: int = 50,
                  open_only: bool = False) -> dict:
    limit = max(1, min(int(limit), 200))
    with db.tx() as c:
        task_id = _thread(c, actor, thread)
        sql = "SELECT m.* FROM messages m WHERE m.project_id=? AND m.task_id IS ? AND m.id > ?"
        if open_only:
            sql += f" AND {_OPEN_SQL}"
        rows = c.execute(sql + " ORDER BY m.id LIMIT ?", (actor.project_id, task_id, since, limit + 1)).fetchall()
        more, rows = len(rows) > limit, rows[:limit]
        if rows:
            c.execute("INSERT INTO read_cursors (participant_id, project_id, task_id, last_message_id) "
                      "VALUES (?, ?, ?, ?) ON CONFLICT (participant_id, project_id, COALESCE(task_id, 0)) "
                      "DO UPDATE SET last_message_id = MAX(last_message_id, excluded.last_message_id)",
                      (actor.id, actor.project_id, task_id, rows[-1]["id"]))
        return {"messages": [_message_json(c, r) for r in rows],
                "next_since": rows[-1]["id"] if more else None}


def resolve_question(db: Database, actor: Participant, message_id: int) -> dict:
    with db.tx() as c:
        row = _message_row(c, actor, message_id)
        if row["kind"] != "question":
            raise GridError(422, "not_a_question", f"#{message_id} isn't a question.")
        if row["author_id"] != actor.id:
            raise GridError(403, "not_your_question", f"Only the asker can resolve #{message_id}. "
                            "Reply to it instead.", f'grid chat send <thread> "<answer>" --reply-to {message_id}')
        c.execute("UPDATE messages SET resolved_at=? WHERE id=?", (now(), message_id))
        return _message_json(c, _message_row(c, actor, message_id))
