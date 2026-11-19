"""Round 6 (fresh-agent stress test): agents put -a before the command, and passed -a to
append, because board help said "Pass -a on every call". Both now work, and help says
where -a goes."""

import re
import sys

from board.help import HELP

ID_RE = re.compile(r"^p_[a-z2-7]{6,8}$")


def ok_id(r):
    assert r.returncode == 0, r.stderr
    assert ID_RE.match(r.stdout.strip()), r.stdout
    return r.stdout.strip()


def test_agent_flag_before_the_command(board, store):
    pid = ok_id(board("-a", "backend", "post", "-t", "Start", "Starting the API"))
    assert store.get_post(pid)["agent"] == "backend"
    pid = ok_id(board("--agent", "backend/server", "post", "up"))
    assert store.get_post(pid)["agent"] == "backend/server"
    pid = ok_id(board("--agent=backend/tests", "post", "x"))
    assert store.get_post(pid)["agent"] == "backend/tests"


def test_agent_flag_before_log(board, store):
    r = board("-a", "lead/tests", "log", "-t", "run", "--", sys.executable, "-c", "print('hi')")
    assert r.returncode == 0, r.stderr
    (p,), _ = store.list_posts()
    assert (p["agent"], p["kind"], p["status"]) == ("lead/tests", "log", "ok")


def test_append_and_wait_accept_and_ignore_agent(board, store):
    pid = ok_id(board("post", "-a", "docs", "first"))
    assert board("append", "-a", "docs", pid, "second").returncode == 0
    assert board("append", pid, "-a", "someone/else", "-s", "ok").returncode == 0
    p = store.get_post(pid)
    assert (p["agent"], p["status"], p["payload"]["text"]) == ("docs", "ok", "first\nsecond")
    r = board("wait", "-a", "docs", pid, "--timeout", "1")
    assert "No such option" not in r.stderr


def test_bare_agent_flag_is_still_a_usage_error(board):
    r = board("-a", "x")
    assert r.returncode == 2


def test_help_shows_agent_after_the_command():
    assert "board -a" not in HELP
    assert "append" in HELP.split("WHO YOU ARE")[1].split("POST")[0]
