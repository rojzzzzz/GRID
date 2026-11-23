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

    return app
