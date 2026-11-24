import json
from concurrent.futures import ThreadPoolExecutor

import httpx

import pytest

from conftest import seed_over_http


# --- Task 5: CLI ---------------------------------------------------------------

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


# --- Task 6: concurrency and the Stage 1 end-to-end check -----------------------

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
    def ok(*args, token=None):
        r = cli(*args, token=token)
        assert r.exit_code == 0, r.stderr
        return r

    def last_line(r):
        return r.stdout.strip().splitlines()[-1]

    def msgs(thread, *extra, token):
        return json.loads(ok("chat", "read", thread, *extra, "--json", token=token).stdout)["messages"]

    human = last_line(ok("init", "--project", "Web app MVP", "--human", "alice"))
    lead = last_line(ok("join", "--role", "lead", "--name", "planner", "--tool", "opus", token=human))
    fe = last_line(ok("join", "--role", "worker", "--name", "frontend", "--tool", "deepseek", token=human))
    ok("join", "--role", "worker", "--name", "backend", "--tool", "codex", token=human)
    rev = last_line(ok("join", "--role", "reviewer", "--name", "checker", "--tool", "codex", token=human))

    ok("task", "create", "Login form", "--scope", "web/login/", token=lead)
    ok("task", "create", "Auth API", "--scope", "api/auth/", token=lead)
    ok("task", "assign", "T-1", "frontend", token=lead)
    ok("task", "assign", "T-2", "backend", token=lead)
    ok("task", "review", "T-1", "--reviewer", "checker", token=lead)

    q = json.loads(ok("chat", "send", "T-1", "@reviewer rate-limit on client?", "--question", "--json",
                      token=fe).stdout)
    ok("chat", "send", "T-1", "No, server side.", "--reply-to", str(q["id"]), token=rev)
    ok("chat", "send", "project", "Use session cookies.", "--decide", token=lead)

    r = cli("chat", "send", "T-1", "@backend can you check the API?", token=fe)
    assert r.exit_code == 1 and "@lead" in r.stderr

    assert [m["text"] for m in msgs("T-1", token=lead)] == ["@reviewer rate-limit on client?", "No, server side."]
    assert [(m["kind"], m["text"]) for m in msgs("project", token=lead)] == [("decision", "Use session cookies.")]
    assert msgs("T-2", token=lead) == []
    assert msgs("T-1", "--open", token=lead) == []


@pytest.mark.parametrize("text", ["hello\rerror: x", "a\r\nerror: x", "a error: x", "a\x85error: x",
                                  "a\x0berror: x", "a\x0cerror: x", "\x1b[2K\x1b[1Gerror: x"])
def test_hostile_text_stays_framed(cli, seed, text):                       # final review fix
    cli("chat", "send", "project", text, token=seed.lead)
    out = cli("chat", "read", "project", token=seed.lead).stdout
    assert "\r" not in out and "\x1b" not in out
    lines = out.splitlines()
    hits = [l for l in lines if "error: x" in l]
    assert hits and all(l.startswith("  │ ") for l in hits)


def test_renderers_neutralize_control_characters():                        # final review fix
    from grid.cli import render_message, render_task
    m = {"id": 1, "kind": "note", "text": "\x1b[2K\x1b[1Gerror: x", "created_at": "now", "reply_to": None,
         "superseded_by": None, "open": False, "author": {"name": "a", "kind": "agent", "role": "lead", "tool": "t\x1b"}}
    out = render_message(m)
    assert "\x1b" not in out and "  │ \\x1b[2K" in out
    t = {"id": "T-1", "title": "x\x1b[2K", "owner": None, "status": "todo", "scope": ["a"], "reviewer": None, "depends_on": []}
    assert "\x1b" not in render_task(t)
