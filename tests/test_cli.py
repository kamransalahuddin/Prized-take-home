"""CLI behavior, exercised through real subprocesses (the way agents call it)."""

import json
import os
import pty
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from board.help import HELP
from board.store import BoardError, Store

BOARD_ARGV = [sys.executable, "-m", "board.cli"]
ID_RE = re.compile(r"^p_[a-z2-7]{6}$")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def ok_id(r):
    assert r.returncode == 0, r.stderr
    pid = r.stdout.strip()
    assert ID_RE.match(pid), r.stdout
    return pid


# ---------------------------------------------------------------- help


@pytest.mark.parametrize(
    "args", [[], ["help"], ["-h"], ["--help"], ["post", "--help"], ["log", "-h"], ["append", "--help"], ["ls", "-h"]]
)
def test_help_everywhere(board, args):
    r = board(*args)
    assert r.returncode == 0
    assert r.stdout.strip() == HELP.strip()
    assert r.stderr == ""


def test_help_fits_a_terminal():
    lines = HELP.rstrip("\n").split("\n")
    assert len(lines) <= 120
    assert max(len(line) for line in lines) <= 80


def test_dashdash_help_goes_to_the_child(board, store):
    r = board("log", "-a", "t", "-q", "--", "echo", "--help")
    assert r.returncode == 0
    (p,), _ = store.list_posts()
    assert store.get_post(p["id"])["payload"]["text"] == "--help\n"


# ---------------------------------------------------------------- post: every kind


def test_post_text(board, store):
    pid = ok_id(board("post", "-a", "lead", "-t", "Hi", "-s", "ok", "Starting the auth refactor"))
    p = store.get_post(pid)
    assert (p["agent"], p["kind"], p["title"], p["status"]) == ("lead", "text", "Hi", "ok")
    assert p["payload"]["text"] == "Starting the auth refactor"


def test_post_text_hidden_alias_and_env_agent(board, store):
    pid = ok_id(board("post", "--text", "hello", env={"BOARD_AGENT": "lead/x"}))
    p = store.get_post(pid)
    assert (p["agent"], p["payload"]["text"]) == ("lead/x", "hello")


def test_post_markdown_stdin(board, store):
    md = "## Auth\n- Tokens never expire\n"
    pid = ok_id(board("post", "-a", "lead/research", "--md", "-", input=md))
    assert store.get_post(pid)["payload"] == {"markdown": md}
    pid = ok_id(board("post", "-a", "lead/research", "--markdown", "# alias"))
    assert store.get_post(pid)["payload"] == {"markdown": "# alias"}


def test_post_table_csv_file_and_json(board, store, tmp_path):
    f = tmp_path / "results.csv"
    f.write_text("test,result\nlogin,pass\nlogout,fail\n")
    pid = ok_id(board("post", "-a", "lead/tests", "--table", f"@{f}"))
    assert store.get_post(pid)["payload"] == {
        "columns": ["test", "result"],
        "rows": [["login", "pass"], ["logout", "fail"]],
    }
    pid = ok_id(board("post", "-a", "lead/tests", "--table", '[["test","result"],["login","pass"]]'))
    assert store.get_post(pid)["payload"]["rows"] == [["login", "pass"]]
    pid = ok_id(board("post", "-a", "lead/tests", "--table", "-", input='[{"a": 1, "b": true}, {"a": 2}]'))
    assert store.get_post(pid)["payload"] == {"columns": ["a", "b"], "rows": [[1, True], [2, None]]}


def test_post_chart(board, store):
    opt = {"xAxis": {"type": "category", "data": ["v1", "v2"]}, "series": [{"type": "line", "data": [1, 2]}]}
    pid = ok_id(board("post", "-a", "lead/perf", "--chart", "-", input=json.dumps(opt)))
    assert store.get_post(pid)["payload"] == {"option": opt}


def test_post_chart_bad_json_reports_position(board):
    r = board("post", "-a", "lead", "--chart", '{"series": [1,]}')
    assert r.returncode == 2
    assert "line 1, col" in r.stderr and r.stderr.startswith("board: ")
    assert "Run 'board help' for usage." in r.stderr
    r = board("post", "-a", "lead", "--chart", "[1, 2]")
    assert r.returncode == 2


def test_post_chart_without_series_is_rejected(board):
    r = board("post", "-a", "lead", "--chart", '{"xAxis": {}}')
    assert r.returncode == 2
    assert "series" in r.stderr


def test_post_image_file_and_stdin(board, store, tmp_path):
    f = tmp_path / "shot.png"
    f.write_bytes(PNG)
    r = board("post", "-a", "lead/ui", "--image", str(f), "--json")
    assert r.returncode == 0, r.stderr
    post = json.loads(r.stdout)
    assert post["kind"] == "image" and post["payload"]["mime"] == "image/png"
    path, mime = store.get_asset(post["payload"]["asset_id"])
    assert path.read_bytes() == PNG and mime == "image/png"
    r = board("post", "-a", "lead/ui", "--image", "-", input=PNG)
    assert r.returncode == 0, r.stderr
    assert store.get_post(r.stdout.decode().strip())["kind"] == "image"


def test_post_image_not_an_image(board, tmp_path):
    f = tmp_path / "x.png"
    f.write_text("hello")
    r = board("post", "-a", "lead", "--image", str(f))
    assert r.returncode == 1
    assert "not an image" in r.stderr


# ---------------------------------------------------------------- value rule + usage errors


def test_value_rule_file(board, store, tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("# Report\nbody\n")
    pid = ok_id(board("post", "-a", "lead", "--md", f"@{f}"))
    assert store.get_post(pid)["payload"]["markdown"] == "# Report\nbody\n"


def test_value_rule_missing_file(board):
    r = board("post", "-a", "lead", "--md", "@nope/missing.md")
    assert r.returncode == 2
    assert "board: file not found: nope/missing.md" in r.stderr


def test_value_rule_empty_stdin(board):
    r = board("post", "-a", "lead", "-")
    assert r.returncode == 2
    assert "board: nothing on stdin" in r.stderr


def test_value_rule_tty_stdin(board_env):
    master, slave = pty.openpty()
    try:
        r = subprocess.run([*BOARD_ARGV, "post", "-a", "lead", "-"], env=board_env, stdin=slave,
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 2 and "nothing on stdin" in r.stderr
        r = subprocess.run([*BOARD_ARGV, "log", "-a", "lead"], env=board_env, stdin=slave,
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 2 and "pipe output into board log" in r.stderr
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize(
    "args, msg",
    [
        (["post", "-a", "lead"], "use exactly one of TEXT, --md, --table, --chart, --image"),
        (["post", "-a", "lead", "hi", "--md", "x"], "use exactly one of"),
        (["post", "hi"], "-a/--agent is required"),
        (["post", "-a", "Bad Name", "hi"], "bad agent name"),
        (["post", "-a", "lead", "-s", "great", "hi"], "bad status"),
        (["post", "-a", "lead", "--bogus", "hi"], "No such option"),
        (["frobnicate"], "No such command"),
        (["append"], "post ID"),
        (["append", "p_aaaaaa"], "give TEXT"),
        (["post", "-a", "lead", "--table", "-"], "nothing on stdin"),
    ],
)
def test_usage_errors_exit_2(board, args, msg):
    r = board(*args)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert r.stdout == ""
    assert r.stderr.startswith("board: ")
    assert msg in r.stderr
    assert r.stderr.rstrip().endswith("Run 'board help' for usage.")


def test_rejections_exit_1(board, store):
    r = board("append", "p_zzzzzz", "hi")
    assert r.returncode == 1 and "board: no post p_zzzzzz" in r.stderr
    pid = ok_id(board("post", "-a", "lead", "--table", "a,b\n1,2"))
    r = board("append", pid, "more")
    assert r.returncode == 1 and "can only append" in r.stderr


# ---------------------------------------------------------------- key, json, quiet


def test_key_upsert_keeps_id(board, store):
    a = ok_id(board("post", "-a", "lead/perf", "-k", "latency", "--chart", '{"series": [{"type": "line", "data": [1]}]}'))
    b = ok_id(board("post", "-a", "lead/perf", "-k", "latency", "--chart", '{"series": [{"type": "line", "data": [2]}]}'))
    assert a == b
    posts, _ = store.list_posts()
    assert len(posts) == 1 and posts[0]["payload"]["option"]["series"][0]["data"] == [2]
    r = board("post", "-a", "lead/perf", "-k", "latency", "--json", "now text")
    post = json.loads(r.stdout)
    assert post["id"] == a and post["created"] is False and post["kind"] == "text"


def test_json_and_quiet(board):
    r = board("post", "-a", "lead", "--json", "hi")
    post = json.loads(r.stdout)
    assert r.stdout.count("\n") == 1 and post["payload"] == {"text": "hi"} and post["created"] is True
    r = board("post", "-a", "lead", "-q", "hi")
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


# ---------------------------------------------------------------- log


def test_log_run_mode_live_and_exit_code(board_env, store):
    proc = subprocess.Popen(
        [*BOARD_ARGV, "log", "-a", "lead/tests", "--", "sh", "-c", "echo a; sleep 0.6; echo b; exit 3"],
        env=board_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    pid = proc.stdout.readline().strip()  # printed before the command finishes
    assert ID_RE.match(pid)
    # While the command is still sleeping, its first line is already on the board.
    deadline = time.time() + 0.55
    seen = None
    while time.time() < deadline:
        p = store.get_post(pid)
        if p["payload"]["text"] == "a\n":
            seen = p
            break
        time.sleep(0.03)
    assert seen is not None, "first line was not posted while the command was running"
    assert seen["live"] is True and proc.poll() is None
    _, err = proc.communicate(timeout=10)
    assert proc.returncode == 3
    assert err == "a\nb\n"  # echoed to stderr
    p = store.get_post(pid)
    assert p["payload"]["text"] == "a\nb\n"
    assert p["payload"]["exit_code"] == 3
    assert p["status"] == "error" and p["live"] is False
    assert p["title"] == "sh -c 'echo a; sleep 0.6; echo b; exit 3'"


def test_log_run_mode_success_strips_ansi_and_merges_stderr(board, store):
    r = board("log", "-a", "lead", "-t", "build", "--", "sh", "-c", r"printf '\033[31mred\033[0m\n'; echo err >&2")
    pid = ok_id(r)
    p = store.get_post(pid)
    assert p["payload"]["text"] == "red\nerr\n"
    assert p["status"] == "ok" and p["payload"]["exit_code"] == 0 and p["title"] == "build"
    assert "\x1b[31mred" in r.stderr  # echo is a byte-for-byte copy


def test_log_command_not_found(board, store):
    r = board("log", "-a", "lead", "--", "definitely-not-a-command-xyz")
    assert r.returncode == 127
    p = store.get_post(r.stdout.strip())
    assert p["status"] == "error" and p["payload"]["exit_code"] == 127


def test_log_pipe_mode(board, store):
    r = board("log", "-a", "lead", "-t", "build", input="line1\nline2\n")
    pid = ok_id(r)
    assert r.stderr == "line1\nline2\n"
    p = store.get_post(pid)
    assert p["payload"]["text"] == "line1\nline2\n" and p["live"] is False and p["status"] == "info"


def test_log_quiet_has_no_echo(board, store):
    r = board("log", "-a", "lead", "-q", "--", "echo", "hi")
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    (p,), _ = store.list_posts()
    assert store.get_post(p["id"])["payload"]["text"] == "hi\n"


def test_log_large_output(board, store):
    r = board("log", "-a", "lead", "-q", "--", "sh", "-c", "seq 1 50000")
    assert r.returncode == 0
    (p,), _ = store.list_posts()
    text = store.get_post(p["id"])["payload"]["text"]
    assert text == "".join(f"{i}\n" for i in range(1, 50001))


def test_log_storage_failure_never_breaks_child(board, board_dir):
    """If the data dir becomes unusable mid-run, the child still runs and is echoed."""
    r = board("log", "-a", "lead", "--", "sh", "-c", f"echo a; rm -rf '{board_dir}'; sleep 0.4; echo b; exit 4")
    assert r.returncode == 4
    assert r.stderr.startswith("a\n") and "b\n" in r.stderr


# ---------------------------------------------------------------- append


def test_append_text_and_status(board, store):
    pid = ok_id(board("post", "-a", "lead", "-t", "Plan", "step 1"))
    assert ok_id(board("append", pid, "step 2")) == pid
    assert store.get_post(pid)["payload"]["text"] == "step 1\nstep 2"
    r = board("append", pid, "-s", "ok", "--json")
    assert r.returncode == 0
    res = json.loads(r.stdout)
    assert res["id"] == pid and res["status"] == "ok"
    assert store.get_post(pid)["payload"]["text"] == "step 1\nstep 2"
    ok_id(board("append", pid, "-t", "New title"))
    assert store.get_post(pid)["title"] == "New title"


def test_append_to_log_adds_newline_and_stdin_streams(board, store, tmp_path):
    pid = ok_id(board("log", "-a", "lead", input="start\n"))
    ok_id(board("append", pid, "one-off"))
    f = tmp_path / "more.txt"
    f.write_text("from file")
    ok_id(board("append", pid, f"@{f}"))
    r = board("append", pid, "-", "-s", "warn", input="streamed\n")
    assert ok_id(r) == pid
    p = store.get_post(pid)
    assert p["payload"]["text"] == "start\none-off\nfrom file\nstreamed\n"
    assert p["status"] == "warn"


# ---------------------------------------------------------------- ls


def test_ls(board, store):
    a = ok_id(board("post", "-a", "lead", "-t", "Plan", "x"))
    b = ok_id(board("post", "-a", "lead/tests", "a long untitled\tmessage " + "y" * 80))
    c = ok_id(board("post", "-a", "other", "--table", "a,b\n1,2"))
    r = board("ls")
    lines = r.stdout.splitlines()
    assert lines[0].split("\t") == [c, "other", "table", "info", "-"]
    assert lines[1].split("\t")[:4] == [b, "lead/tests", "text", "info"]
    assert lines[1].split("\t")[4].startswith("a long untitled message y")
    assert len(lines[1].split("\t")[4]) <= 60
    assert lines[2].split("\t") == [a, "lead", "text", "info", "Plan"]
    assert [ln.split("\t")[0] for ln in board("ls", "-a", "lead").stdout.splitlines()] == [b, a]
    assert len(board("ls", "-n", "1").stdout.splitlines()) == 1
    js = json.loads(board("ls", "--json").stdout)
    assert [p["id"] for p in js] == [c, b, a] and "payload" not in js[0]


# ---------------------------------------------------------------- concurrency


def test_ten_processes_post_concurrently(board_env, store):
    def worker(w):
        ids = []
        for i in range(10):
            r = subprocess.run([*BOARD_ARGV, "post", "-a", f"w{w}", f"post {i}"], env=board_env,
                               capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
            assert r.returncode == 0, r.stderr
            ids.append(r.stdout.strip())
        return ids

    with ThreadPoolExecutor(10) as ex:
        results = list(ex.map(worker, range(10)))
    posts, seq = store.list_posts(limit=1000)
    assert len(posts) == 100
    seqs = [p["seq"] for p in posts]
    assert len(set(seqs)) == 100 and seq == max(seqs)
    by_id = {p["id"]: p for p in posts}
    for w, ids in enumerate(results):
        assert len(set(ids)) == 10
        worker_seqs = [by_id[i]["seq"] for i in ids]
        assert worker_seqs == sorted(worker_seqs)  # each process's posts in order
        assert [by_id[i]["payload"]["text"] for i in ids] == [f"post {i}" for i in range(10)]
    events = store.events_after(0, limit=1000)
    assert [e["id"] for e in events] == sorted(e["id"] for e in events)


# ---------------------------------------------------------------- BOARD_URL (HTTP backend)


class _StubHandler(BaseHTTPRequestHandler):
    """Minimal stand-in for board/server.py's API (contract section 4), backed by a Store."""

    store: Store
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        with self.lock:
            if self.path.startswith("/api/posts?") or self.path == "/api/posts":
                posts, seq = self.store.list_posts()
                return self._send(200, {"posts": posts, "seq": seq})
            try:
                return self._send(200, self.store.get_post(self.path.rsplit("/", 1)[1]))
            except BoardError as e:
                return self._send(e.status, {"error": e.message})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        with self.lock:
            try:
                if self.path == "/api/posts":
                    post, created = self.store.create_post(**body)
                    return self._send(201 if created else 200, dict(post, created=created))
                m = re.match(r"^/api/posts/([^/]+)/append$", self.path)
                if m:
                    return self._send(200, self.store.append(m.group(1), **body))
                return self._send(404, {"error": "not found"})
            except BoardError as e:
                return self._send(e.status, {"error": e.message})


@pytest.fixture
def stub_url(tmp_path):
    s = Store(tmp_path / "remote")
    handler = type("H", (_StubHandler,), {"store": s})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", s, srv
    srv.shutdown()
    srv.server_close()
    s.close()


def test_board_url_posts_over_http(board, store, stub_url):
    url, remote, _ = stub_url
    env = {"BOARD_URL": url}
    pid = ok_id(board("post", "-a", "lead", "-k", "k", "hi", env=env))
    assert ok_id(board("post", "-a", "lead", "-k", "k", "again", env=env)) == pid
    r = board("log", "-a", "lead", "--", "sh", "-c", "echo x; exit 2", env=env)
    assert r.returncode == 2
    lid = r.stdout.strip()
    assert ok_id(board("append", lid, "-s", "warn", env=env)) == lid
    assert remote.get_post(pid)["payload"]["text"] == "again"
    assert remote.get_post(lid)["payload"]["text"] == "x\n" and remote.get_post(lid)["status"] == "warn"
    assert board("append", "p_zzzzzz", "x", env=env).returncode == 1
    assert len(board("ls", env=env).stdout.splitlines()) == 2
    assert store.list_posts()[0] == []  # nothing written locally


def test_board_url_unreachable_exits_3(board):
    r = board("post", "-a", "lead", "hi", env={"BOARD_URL": "http://127.0.0.1:9"})
    assert r.returncode == 3
    assert r.stderr.startswith("board: ") and "BOARD_URL" in r.stderr


# ---------------------------------------------------------------- startup


def test_post_does_not_import_heavy_modules(board_env):
    code = (
        "import sys; sys.argv=['board','post','-a','lead','hi'];"
        "import board.cli as c;"
        "rc = c.run(sys.argv[1:]);"
        "heavy=[m for m in ('httpx','fastapi','uvicorn') if m in sys.modules];"
        "print(rc, heavy)"
    )
    r = subprocess.run([BOARD_ARGV[0], "-c", code], env=board_env, capture_output=True, text=True, timeout=30)
    assert r.stdout.strip().endswith("0 []"), r.stdout + r.stderr


def test_log_survives_board_url_going_away(board_env, stub_url):
    url, remote, srv = stub_url
    env = dict(board_env, BOARD_URL=url, BOARD_FINISH_PATIENCE="1")
    proc = subprocess.Popen(
        [*BOARD_ARGV, "log", "-a", "lead", "--", "sh", "-c", "echo a; sleep 0.5; echo b; exit 5"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    pid = proc.stdout.readline().strip()
    assert ID_RE.match(pid)
    deadline = time.time() + 0.45
    while time.time() < deadline and remote.get_post(pid)["payload"]["text"] != "a\n":
        time.sleep(0.03)
    srv.shutdown()
    srv.server_close()  # the board goes away mid-run
    _, err = proc.communicate(timeout=20)
    assert proc.returncode == 5
    assert err.startswith("a\n") and "b\n" in err and "board:" in err


# ---- regressions from the CLI validation pass (docs/test-report.md)


def test_text_starting_with_dash_is_text(board, store):
    r = board("post", "-a", "lead", "-3 tests failed", "-s", "error")
    assert r.returncode == 0, r.stderr
    p = store.get_post(r.stdout.strip())
    assert p["payload"]["text"] == "-3 tests failed" and p["status"] == "error"
    md = board("post", "-a", "lead", "--md", "# notes").stdout.strip()
    r = board("append", md, "- fixed login")
    assert r.returncode == 0, r.stderr
    assert store.get_post(md)["payload"]["markdown"].endswith("- fixed login")


def test_at_text_that_is_not_a_path_is_literal(board, store):
    r = board("post", "-a", "lead", "-s", "blocked", "@human need the DB password")
    assert r.returncode == 0, r.stderr
    assert store.get_post(r.stdout.strip())["payload"]["text"] == "@human need the DB password"
    r = board("post", "-a", "lead", "--md", "@missing/report.md")
    assert r.returncode == 2 and "file not found" in r.stderr


def test_single_line_table_rejected(board):
    r = board("post", "-a", "lead", "--table", "just text")
    assert r.returncode == 2 and "header row" in r.stderr


def test_serve_port_in_use_is_clean_error(board):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    try:
        r = board("serve", "--port", str(port))
    finally:
        s.close()
    assert r.returncode == 1 and "pick another with --port" in r.stderr
    assert r.stdout == ""
    r = board("serve", "--port", "99999")
    assert r.returncode == 2 and "Traceback" not in r.stderr


def test_log_stops_when_post_replaced_by_key(board, board_env, store):
    import time as _t
    p = subprocess.Popen([sys.executable, "-m", "board.cli", "log", "-a", "lead", "-k", "job", "--", "sh", "-c",
                          "for i in 1 2 3 4 5 6; do echo L$i; sleep 0.2; done; exit 3"],
                         env=board_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    pid = p.stdout.readline().strip()
    _t.sleep(0.6)
    assert board("post", "-a", "lead", "-k", "job", "replaced").stdout.strip() == pid
    p.communicate(timeout=10)
    assert p.returncode == 3  # child's code still passes through
    post = store.get_post(pid)
    assert post["kind"] == "text" and post["payload"] == {"text": "replaced"}


def test_empty_append_is_usage_error(board):
    pid = board("post", "-a", "lead", "x").stdout.strip()
    r = board("append", pid, "")
    assert r.returncode == 2


def test_log_end_delivered_after_server_restart(board_env, store, tmp_path):
    """BOARD_URL log whose server is down when the child exits: the tail and exit code
    arrive once the server is back (restart-recovery report, bug 2)."""
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    url = f"http://127.0.0.1:{port}"
    serve = [*BOARD_ARGV, "serve", "--port", str(port)]
    def start():
        p = subprocess.Popen(serve, env=board_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
                return p
            except OSError:
                time.sleep(0.05)
        raise AssertionError("server did not start")
    srv = start()
    log = subprocess.Popen(
        [*BOARD_ARGV, "log", "-a", "lead", "--", "sh", "-c",
         "for i in 1 2 3 4 5 6 7 8; do echo L$i; sleep 0.2; done; exit 5"],
        env=dict(board_env, BOARD_URL=url), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    pid = log.stdout.readline().strip()
    time.sleep(0.5)
    srv.terminate(); srv.wait(5)
    time.sleep(2.5)  # child finishes while the server is down
    srv = start()
    try:
        log.communicate(timeout=30)
        assert log.returncode == 5
        p = store.get_post(pid)
        assert p["payload"]["text"] == "".join(f"L{i}\n" for i in range(1, 9))
        assert p["payload"]["exit_code"] == 5 and p["status"] == "error" and p["live"] is False
    finally:
        srv.terminate(); srv.wait(5)


def test_serve_restarts_immediately_after_kill9_with_open_client(board_env):
    """Restart-recovery report, bug 1: lingering connections must not block a restart."""
    import signal as _sig
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    def start():
        p = subprocess.Popen([*BOARD_ARGV, "serve", "--port", str(port)], env=board_env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        for _ in range(100):
            try:
                return p, socket.create_connection(("127.0.0.1", port), timeout=0.1)
            except OSError:
                if p.poll() is not None:
                    raise AssertionError(p.stderr.read())
                time.sleep(0.05)
        raise AssertionError("server did not start")
    srv, conn = start()
    conn.sendall(b"GET /api/events HTTP/1.1\r\nHost: x\r\n\r\n"); time.sleep(0.3)
    srv.send_signal(_sig.SIGKILL); srv.wait(5)
    srv2, conn2 = start()  # must not fail with "Address already in use"
    conn.close(); conn2.close(); srv2.terminate(); srv2.wait(5)
