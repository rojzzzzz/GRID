import json

import pytest


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
