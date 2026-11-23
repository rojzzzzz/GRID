"""GRID command line: a thin client over the GRID service.

Text output frames every line of message text with '  │ ' so another participant's
words can never pass for GRID's own output. Every command takes --json.
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Callable, Optional

import httpx
import typer

DEFAULT_URL = "http://127.0.0.1:7420"
FRAME = "  │ "

app = typer.Typer(no_args_is_help=True, add_completion=False, help="GRID: shared threads for humans and agents.")
project_app = typer.Typer(no_args_is_help=True, help="Project settings.")
task_app = typer.Typer(no_args_is_help=True, help="Tasks: create, scope, assign, review.")
chat_app = typer.Typer(no_args_is_help=True, help="Threads: send, read, resolve.")
app.add_typer(project_app, name="project")
app.add_typer(task_app, name="task")
app.add_typer(chat_app, name="chat")

JsonOpt = typer.Option(False, "--json", help="Print the raw API response.")


# --- client -------------------------------------------------------------------

_factory: Optional[Callable[[], httpx.Client]] = None


def set_client_factory(f: Optional[Callable[[], httpx.Client]]) -> None:
    """Tests point the CLI at an in-process app; None restores the real HTTP client."""
    global _factory
    _factory = f


def _url() -> str:
    return os.environ.get("GRID_URL", DEFAULT_URL)


def _fail(message: str, fix: str | None = None) -> None:
    typer.echo(f"error: {_visible(message)}", err=True)
    if fix:
        typer.echo(f"  fix: {_visible(fix)}", err=True)
    raise typer.Exit(1)


def call(method: str, path: str, json: dict | None = None, params: dict | None = None) -> dict:
    token = os.environ.get("GRID_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    client = _factory() if _factory else httpx.Client(base_url=_url(), timeout=30)
    try:
        r = client.request(method, path, json=json, params=params, headers=headers)
    except httpx.TransportError:
        _fail(f"GRID service not reachable at {_url()}", "grid serve")
    finally:
        if not _factory:
            client.close()
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400:
        if isinstance(body, dict) and "error" in body:
            _fail(body["message"], body.get("fix"))
        _fail(f"GRID service returned HTTP {r.status_code}")
    return body


def _out(data: dict, as_json: bool, text: Callable[[dict], str]) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2) if as_json else text(data))


# --- rendering ------------------------------------------------------------------

# Anything that could move the cursor, erase the frame, or start a new line in a terminal
# or an agent's reader. Tabs are kept. Line breaks are handled by str.splitlines() first.
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f  ]")


def _visible(s: str) -> str:
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", str(s))


def frame(text: str) -> str:
    """Every line of participant-written text gets the frame; nothing can escape it."""
    return "\n".join(FRAME + _visible(line) for line in (text.splitlines() or [""]))


def render_message(m: dict) -> str:
    a = {k: _visible(v) if isinstance(v, str) else v for k, v in m["author"].items()}
    parts = [f"#{m['id']}  @{a['name']} ({a['tool']})", a["kind"], m["kind"]]
    if m["kind"] == "decision":
        parts.append(f"D-{m['id']}")
    if m.get("reply_to"):
        parts.append(f"reply to #{m['reply_to']}")
    if m.get("superseded_by"):
        parts.append(f"superseded by D-{m['superseded_by']}")
    if m.get("open"):
        parts.append("open")
    parts.append(m["created_at"])
    return " · ".join(parts) + "\n" + frame(m["text"])


def render_task(t: dict) -> str:
    owner = f"@{t['owner']}" if t["owner"] else "—"
    scope = _visible(" ".join(t["scope"])) or "none"
    line = f"{t['id']}  {_visible(t['title'])}  {owner}  {t['status']}  scope: {scope}"
    if t.get("reviewer"):
        line += f"  reviewer: @{t['reviewer']}"
    if t.get("depends_on"):
        line += f"  depends on: {' '.join(t['depends_on'])}"
    return line


def render_participant(p: dict) -> str:
    role = f" · {p['role']}" if p.get("role") else ""
    revoked = " · revoked" if p.get("active") is False else ""
    return _visible(f"@{p['name']} ({p['tool']}) · {p['kind']}{role}{revoked}")


def _joined(data: dict) -> str:
    return f"Registered {render_participant(data['participant'])}\n{data['token']}"


@app.callback()
def _setup() -> None:
    # Agents usually read GRID through a pipe; make sure the frame characters survive it.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# --- identity -------------------------------------------------------------------

@app.command()
def init(project: str = typer.Option(..., "--project", help="Project name."),
         human: str = typer.Option(..., "--human", help="Your name (the first human)."),
         as_json: bool = JsonOpt):
    """Create the project and the first human. Prints that human's token last."""
    _out(call("POST", "/init", {"project": project, "human": human}), as_json, _joined)


@app.command()
def serve(db: Path = typer.Option(Path("grid.db"), "--db"), host: str = typer.Option("127.0.0.1", "--host"),
          port: int = typer.Option(7420, "--port")):
    """Run the GRID service (the only writer to the database)."""
    import uvicorn

    from grid.server import create_app

    uvicorn.run(create_app(db), host=host, port=port, log_level="warning")


@app.command("join")
def join_cmd(name: str = typer.Option(..., "--name"), role: Optional[str] = typer.Option(None, "--role"),
             human: bool = typer.Option(False, "--human"), tool: str = typer.Option("", "--tool"),
             as_json: bool = JsonOpt):
    """Register a participant (humans only). Prints the new token last."""
    body = {"name": name, "role": role, "human": human, "tool": tool}
    _out(call("POST", "/participants", body), as_json, _joined)


@app.command()
def revoke(name: str, as_json: bool = JsonOpt):
    """Deactivate a participant and its token (humans only)."""
    _out(call("DELETE", f"/participants/{name.lstrip('@')}"), as_json, lambda p: f"Revoked @{p['name']}")


@app.command()
def whoami(as_json: bool = JsonOpt):
    """Show who this token belongs to."""
    _out(call("GET", "/whoami"), as_json, render_participant)


@app.command()
def participants(as_json: bool = JsonOpt):
    """List everyone in the project."""
    _out(call("GET", "/participants"), as_json,
         lambda d: "\n".join(render_participant(p) for p in d["participants"]))


@app.command()
def activity(task: Optional[str] = typer.Argument(None), as_json: bool = JsonOpt):
    """Show the activity log, for the project or one task."""
    def text(d):
        return "\n".join(_visible(f"{e['created_at']}  @{e['actor']}  {e['action']}  {e['task'] or ''}  "
                                  f"{json.dumps(e['detail'], ensure_ascii=False)}") for e in d["activity"])
    _out(call("GET", "/activity", params={"task": task} if task else None), as_json, text)


# --- project ------------------------------------------------------------------

def _settings_text(d: dict) -> str:
    return "\n".join(f"{k} = {v}" for k, v in d.items())


@project_app.command("settings")
def project_settings(as_json: bool = JsonOpt):
    """Show project settings."""
    _out(call("GET", "/project/settings"), as_json, _settings_text)


@project_app.command("set")
def project_set(key: str, value: int, as_json: bool = JsonOpt):
    """Change a project setting (lead and humans)."""
    _out(call("PATCH", "/project/settings", {"key": key, "value": value}), as_json, _settings_text)


# --- tasks --------------------------------------------------------------------

@task_app.command("create")
def task_create(title: str, goal: str = typer.Option("", "--goal"),
                scope: list[str] = typer.Option([], "--scope", help="Paths this task touches."),
                as_json: bool = JsonOpt):
    """Create a task (lead and humans)."""
    _out(call("POST", "/tasks", {"title": title, "goal": goal, "scope": scope}), as_json, render_task)


@task_app.command("scope")
def task_scope(task: str, paths: list[str], allow_overlap: bool = typer.Option(False, "--allow-overlap"),
               as_json: bool = JsonOpt):
    """Replace a task's scope (lead and humans)."""
    _out(call("PUT", f"/tasks/{task}/scope", {"scope": paths, "allow_overlap": allow_overlap}),
         as_json, render_task)


@task_app.command("assign")
def task_assign(task: str, owner: str, allow_overlap: bool = typer.Option(False, "--allow-overlap"),
                as_json: bool = JsonOpt):
    """Assign a task to a worker (lead and humans)."""
    _out(call("POST", f"/tasks/{task}/assign", {"owner": owner.lstrip("@"), "allow_overlap": allow_overlap}),
         as_json, render_task)


@task_app.command("depends")
def task_depends(task: str, on: str = typer.Option(..., "--on"), as_json: bool = JsonOpt):
    """Make a task wait for another (lead and humans)."""
    _out(call("POST", f"/tasks/{task}/depends", {"on": on}), as_json, render_task)


@task_app.command("review")
def task_review(task: str, reviewer: str = typer.Option(..., "--reviewer"), as_json: bool = JsonOpt):
    """Assign a task's reviewer (lead and humans)."""
    _out(call("POST", f"/tasks/{task}/reviewer", {"reviewer": reviewer.lstrip("@")}), as_json, render_task)


@task_app.command("status")
def task_status(task: str, status: str, as_json: bool = JsonOpt):
    """Override a task's status (humans only)."""
    _out(call("POST", f"/tasks/{task}/status", {"status": status}), as_json, render_task)


@task_app.command("list")
def task_list(as_json: bool = JsonOpt):
    """List tasks."""
    _out(call("GET", "/tasks"), as_json, lambda d: "\n".join(render_task(t) for t in d["tasks"]) or "No tasks.")


# --- chat ---------------------------------------------------------------------

def _message_id(s: Optional[str]) -> Optional[int]:
    if s is None:
        return None
    try:
        return int(s.strip().lstrip("#").removeprefix("D-").removeprefix("d-"))
    except ValueError:
        _fail(f"'{s}' isn't a message id. Use the form 12, #12 or D-12.")


@chat_app.command("send")
def chat_send(thread: str, text: str,
              question: bool = typer.Option(False, "--question"), propose: bool = typer.Option(False, "--propose"),
              decide: bool = typer.Option(False, "--decide"),
              supersedes: Optional[str] = typer.Option(None, "--supersedes"),
              reply_to: Optional[str] = typer.Option(None, "--reply-to"),
              idempotency_key: Optional[str] = typer.Option(None, "--idempotency-key"),
              as_json: bool = JsonOpt):
    """Post to 'project' or a task thread like T-3."""
    kinds = [k for k, on in (("question", question), ("proposal", propose), ("decision", decide)) if on]
    if len(kinds) > 1:
        _fail("Pick at most one of --question, --propose, --decide.")
    body = {"text": text, "kind": kinds[0] if kinds else "note", "reply_to": _message_id(reply_to),
            "supersedes": _message_id(supersedes), "idempotency_key": idempotency_key or str(uuid.uuid4())}
    _out(call("POST", f"/threads/{thread}/messages", body), as_json, render_message)


@chat_app.command("read")
def chat_read(thread: str, since: int = typer.Option(0, "--since"), limit: int = typer.Option(50, "--limit"),
              open_only: bool = typer.Option(False, "--open", help="Only open questions."),
              as_json: bool = JsonOpt):
    """Read a thread, oldest first."""
    params = {"since": since, "limit": limit, "open": open_only}

    def text(d):
        out = "\n\n".join(render_message(m) for m in d["messages"]) or "No messages."
        if d["next_since"]:
            out += f"\n\nMore: grid chat read {thread} --since {d['next_since']}"
        return out
    _out(call("GET", f"/threads/{thread}/messages", params=params), as_json, text)


@chat_app.command("resolve")
def chat_resolve(message: str, as_json: bool = JsonOpt):
    """Close your own open question."""
    _out(call("POST", f"/messages/{_message_id(message)}/resolve"), as_json, render_message)


if __name__ == "__main__":
    app()
