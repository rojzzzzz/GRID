"""GRID HTTP service: maps routes to core functions. The only writer to the database."""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from grid import core
from grid.core import GridError, Participant


def create_app(db_path: Path) -> FastAPI:
    app = FastAPI(title="GRID")
    app.state.db = db = core.Database(db_path)

    @app.exception_handler(GridError)
    def grid_error(_request: Request, exc: GridError):
        return JSONResponse(exc.to_json(), status_code=exc.status)

    def me(request: Request) -> Participant:
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        return core.authenticate(db, token)

    @app.get("/health")
    def health():
        return {"ok": True}

    # identity
    @app.post("/init")
    def init(body: dict):
        p, token = core.init_project(db, body.get("project", ""), body.get("human", ""))
        return {"participant": p.to_json(), "token": token}

    @app.post("/participants")
    def join(body: dict, actor: Participant = Depends(me)):
        p, token = core.join(db, actor, name=body.get("name", ""), role=body.get("role"),
                             human=bool(body.get("human")), tool=body.get("tool", ""))
        return {"participant": p.to_json(), "token": token}

    @app.delete("/participants/{name}")
    def revoke(name: str, actor: Participant = Depends(me)):
        return core.revoke(db, actor, name).to_json()

    @app.get("/participants")
    def participants(actor: Participant = Depends(me)):
        return {"participants": core.list_participants(db, actor)}

    @app.get("/whoami")
    def whoami(actor: Participant = Depends(me)):
        return actor.to_json()

    @app.get("/activity")
    def activity(task: str | None = None, actor: Participant = Depends(me)):
        tid = core.parse_task_id(task) if task else None
        return {"activity": core.list_activity(db, actor, tid)}

    # settings
    @app.get("/project/settings")
    def settings(actor: Participant = Depends(me)):
        return core.get_settings(db, actor)

    @app.patch("/project/settings")
    def set_setting(body: dict, actor: Participant = Depends(me)):
        return core.set_setting(db, actor, body.get("key", ""), body.get("value"))

    # tasks
    @app.post("/tasks")
    def create_task(body: dict, actor: Participant = Depends(me)):
        return core.create_task(db, actor, body.get("title", ""), body.get("goal", ""), body.get("scope") or [])

    @app.get("/tasks")
    def list_tasks(actor: Participant = Depends(me)):
        return {"tasks": core.list_tasks(db, actor)}

    @app.get("/tasks/{tid}")
    def get_task(tid: str, actor: Participant = Depends(me)):
        return core.get_task(db, actor, core.parse_task_id(tid))

    @app.put("/tasks/{tid}/scope")
    def set_scope(tid: str, body: dict, actor: Participant = Depends(me)):
        return core.set_scope(db, actor, core.parse_task_id(tid), body.get("scope") or [],
                              bool(body.get("allow_overlap")))

    @app.post("/tasks/{tid}/assign")
    def assign(tid: str, body: dict, actor: Participant = Depends(me)):
        return core.assign_task(db, actor, core.parse_task_id(tid), body.get("owner", ""),
                                bool(body.get("allow_overlap")))

    @app.post("/tasks/{tid}/depends")
    def depends(tid: str, body: dict, actor: Participant = Depends(me)):
        return core.add_dependency(db, actor, core.parse_task_id(tid), core.parse_task_id(body.get("on", "")))

    @app.post("/tasks/{tid}/reviewer")
    def reviewer(tid: str, body: dict, actor: Participant = Depends(me)):
        return core.set_reviewer(db, actor, core.parse_task_id(tid), body.get("reviewer", ""))

    @app.post("/tasks/{tid}/status")
    def status(tid: str, body: dict, actor: Participant = Depends(me)):
        return core.set_status(db, actor, core.parse_task_id(tid), body.get("status", ""))

    # messages (no route edits a message: decisions are superseded, never changed)
    @app.post("/threads/{thread}/messages")
    def send(thread: str, body: dict, actor: Participant = Depends(me)):
        return core.send_message(db, actor, thread, body.get("text", ""), body.get("kind") or "note",
                                 body.get("reply_to"), body.get("supersedes"), body.get("idempotency_key"))

    @app.get("/threads/{thread}/messages")
    def read(thread: str, since: int = 0, limit: int = 50, open: bool = False,
             actor: Participant = Depends(me)):
        return core.read_messages(db, actor, thread, since, limit, open)

    @app.post("/messages/{mid}/resolve")
    def resolve(mid: int, actor: Participant = Depends(me)):
        return core.resolve_question(db, actor, mid)

    return app
