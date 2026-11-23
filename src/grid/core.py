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
