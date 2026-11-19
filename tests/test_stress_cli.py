"""CLI regressions from the stress QA passes: duplicate writes under a flaky network,
replaced posts/questions, signals and process groups."""

from __future__ import annotations

import collections
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from board.store import BoardError, Store

BOARD_ARGV = [sys.executable, "-m", "board.cli"]
ID_RE = re.compile(r"^p_[a-z2-7]{6,8}$")
ASK_LINE = re.compile(r"^board: waiting for the human on (p_[a-z2-7]{6,8}) ")


def spawn(board_env, *args, env=None, **kw):
    kw.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.Popen([*BOARD_ARGV, *args], env=dict(board_env, **(env or {})),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)


# ---------------------------------------------------------------- chaos server


class ChaosHandler(BaseHTTPRequestHandler):
    """The board API over a Store, with per-request misbehaviour chosen by `plan(path,
    rid, n)` (n = how many times this request_id was seen before):
      ok        answer normally
      drop      commit, then close the connection without answering
      502after  commit, then answer 502 (a proxy that lost the upstream response)
      502       answer 502 without committing
      late      close the connection now; commit 1 s later (a request stuck in a proxy)
    """

    store: Store
    lock: threading.Lock
    seen: collections.Counter
    plan = staticmethod(lambda path, rid, n: "ok")

    def log_message(self, *a):
        pass

    def _send(self, code, obj, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        with self.lock:
            try:
                if self.path.startswith("/api/posts?") or self.path == "/api/posts":
                    posts, seq = self.store.list_posts()
                    return self._send(200, {"posts": posts, "seq": seq})
                return self._send(200, self.store.get_post(self.path.rsplit("/", 1)[1]))
            except BoardError as e:
                return self._send(e.status, {"error": e.message})

    def _apply(self, path, body):
        with self.lock:
            try:
                if path == "/api/posts":
                    post, created = self.store.create_post(**body)
                    return 201 if created else 200, dict(post, created=created)
                m = re.match(r"^/api/posts/([^/]+)/(append|reply)$", path)
                if m and m.group(2) == "append":
                    return 200, self.store.append(m.group(1), **body)
                if m:
                    return 200, self.store.reply(m.group(1), **body)
                return 404, {"error": "not found"}
            except BoardError as e:
                return e.status, {"error": e.message}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        rid = body.get("request_id")
        with self.lock:
            n = self.seen[rid]
            self.seen[rid] += 1
        mode = self.plan(self.path, rid, n)
        if mode == "502":
            return self._send(502, b"Bad Gateway", "text/html")
        if mode == "late":
            path = self.path
            threading.Timer(1.0, lambda: self._apply(path, body)).start()
            self.close_connection = True
            return
        res = self._apply(self.path, body)
        if mode == "drop":
            self.close_connection = True
            return
        if mode == "502after":
            return self._send(502, b"Bad Gateway", "text/html")
        self._send(*res)


@pytest.fixture
def chaos(tmp_path):
    s = Store(tmp_path / "remote")
    handler = type("C", (ChaosHandler,), {"store": s, "lock": threading.Lock(), "seen": collections.Counter()})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", s, handler
    srv.shutdown()
    srv.server_close()
    s.close()


def run_board(board_env, *args, env=None, input=None, timeout=60):
    e = dict(board_env, **(env or {}))
    kw = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run([*BOARD_ARGV, *args], env=e, capture_output=True, text=True, timeout=timeout, **kw)


def texts(store):
    return [p["payload"].get("text") for p in store.list_posts()[0]]


# ---------------------------------------------------------------- finding 2: give up too early


def test_one_shot_write_outlasts_a_late_commit(board_env, chaos):
    url, remote, h = chaos
    h.plan = staticmethod(lambda path, rid, n: "late" if n < 4 else "ok")  # 4 lost tries over ~2 s
    r = run_board(board_env, "post", "-a", "lead", "once", env={"BOARD_URL": url})
    time.sleep(1.2)  # let every late commit land
    assert r.returncode == 0, r.stderr
    assert texts(remote) == ["once"]  # exactly one post, and it is the one printed
    assert remote.list_posts()[0][0]["id"] == r.stdout.strip()


def test_log_post_created_late_receives_all_output(board_env, chaos):
    url, remote, h = chaos
    h.plan = staticmethod(lambda path, rid, n: "late" if path == "/api/posts" and n < 3 else "ok")
    r = run_board(board_env, "log", "-a", "lead", "--", "sh", "-c",
                  "for i in 1 2 3 4 5; do echo L$i; sleep 0.1; done; exit 4", env={"BOARD_URL": url})
    time.sleep(1.2)
    assert r.returncode == 4, r.stderr
    posts = remote.list_posts()[0]
    assert len(posts) == 1, posts
    p = remote.get_post(posts[0]["id"])
    assert r.stdout.strip() == p["id"]
    assert p["payload"]["text"] == "".join(f"L{i}\n" for i in range(1, 6))
    assert p["live"] is False and p["payload"]["exit_code"] == 4 and p["status"] == "error"
    assert "without posting" not in r.stderr


# ---------------------------------------------------------------- finding 3: 5xx is transient


def test_502_after_commit_is_retried_not_fatal(board_env, chaos):
    url, remote, h = chaos
    h.plan = staticmethod(lambda path, rid, n: "502after" if n == 0 else "ok")
    r = run_board(board_env, "post", "-a", "lead", "x", env={"BOARD_URL": url})
    assert r.returncode == 0, r.stderr
    r = run_board(board_env, "log", "-a", "lead", "--", "sh", "-c",
                  "for i in 1 2 3 4 5 6; do echo L$i; sleep 0.15; done", env={"BOARD_URL": url})
    assert r.returncode == 0, r.stderr
    lid = r.stdout.strip()
    p = remote.get_post(lid)
    assert p["payload"]["text"] == "".join(f"L{i}\n" for i in range(1, 7))  # nothing lost, nothing twice
    assert p["live"] is False and "stopped posting" not in r.stderr
    assert len(remote.list_posts()[0]) == 2


def test_502_before_commit_is_retried(board_env, chaos):
    url, remote, h = chaos
    h.plan = staticmethod(lambda path, rid, n: "502" if n < 2 else "ok")
    r = run_board(board_env, "log", "-a", "lead", "--", "sh", "-c", "echo a; sleep 0.3; echo b",
                  env={"BOARD_URL": url})
    assert r.returncode == 0, r.stderr
    assert remote.get_post(r.stdout.strip())["payload"]["text"] == "a\nb\n"


def test_persistent_5xx_exits_1_after_patience(board_env, chaos):
    url, remote, h = chaos
    h.plan = staticmethod(lambda path, rid, n: "502")
    r = run_board(board_env, "post", "-a", "lead", "x", env={"BOARD_URL": url, "BOARD_WRITE_PATIENCE": "1"})
    assert r.returncode == 1 and "502" in r.stderr and "Traceback" not in r.stderr


# ---------------------------------------------------------------- finding 4: log replaced by another log


def test_log_replaced_by_second_log_with_same_key_stops_posting(board_env, store):
    first = spawn(board_env, "log", "-a", "lead", "-k", "job", "--", "sh", "-c",
                  "for i in 1 2 3 4 5 6 7 8; do echo OLD$i; sleep 0.2; done; exit 3")
    pid = first.stdout.readline().strip()
    time.sleep(0.5)
    second = spawn(board_env, "log", "-a", "lead", "-k", "job", "--", "sh", "-c",
                   "for i in 1 2 3 4 5 6 7 8 9 10 11 12; do echo NEW$i; sleep 0.2; done")
    assert second.stdout.readline().strip() == pid
    _, err1 = first.communicate(timeout=20)
    assert first.returncode == 3  # child kept running, exit code passed through
    assert "OLD8" in err1  # still echoed
    assert "replaced" in err1
    p = store.get_post(pid)
    assert p["live"] is True and "OLD" not in p["payload"]["text"]  # old run's end didn't close it
    second.communicate(timeout=20)
    p = store.get_post(pid)
    assert p["payload"]["text"] == "".join(f"NEW{i}\n" for i in range(1, 13))
    assert p["live"] is False and p["status"] == "ok"


# ---------------------------------------------------------------- finding 6: concurrent ask -k


def ask_id(proc):
    line = proc.stderr.readline()
    m = ASK_LINE.match(line)
    assert m, line
    return m.group(1)


def test_replaced_question_waiter_exits_and_new_one_gets_answer(board_env, store):
    a = spawn(board_env, "ask", "-a", "lead", "-k", "q", "Restart staging?")
    pid = ask_id(a)
    b = spawn(board_env, "ask", "-a", "lead", "-k", "q", "DROP prod DB?")
    assert ask_id(b) == pid
    out_a, err_a = a.communicate(timeout=10)
    assert a.returncode == 1 and out_a == ""
    assert f"your question was replaced by a newer one (board wait {pid})" in err_a
    w = spawn(board_env, "wait", pid)
    time.sleep(0.6)
    store.reply(pid, "reject", "no way")
    assert b.communicate(timeout=10)[0] == "reject\nno way\n"
    assert w.communicate(timeout=10)[0] == "reject\nno way\n"


def test_wait_exits_when_question_replaced(board_env, board, store):
    pid = board("ask", "-a", "lead", "-k", "q", "--timeout", "0", "v1").stderr
    pid = ASK_LINE.match(pid).group(1)
    w = spawn(board_env, "wait", pid)
    time.sleep(0.7)
    store.create_post("lead", "text", {"text": "v2", "ask": True}, status="blocked", key="q")
    out, err = w.communicate(timeout=10)
    assert w.returncode == 1 and out == "" and "replaced" in err


# ---------------------------------------------------------------- batch 4: signals / process groups


def wait_text(store, pid, needle, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if needle in store.get_post(pid)["payload"]["text"]:
            return
        time.sleep(0.05)
    raise AssertionError(f"{needle!r} never appeared")


def test_log_returns_when_child_exits_even_if_grandchild_holds_pipe(board_env, store):
    t0 = time.monotonic()
    r = run_board(board_env, "log", "-a", "t", "--", "sh", "-c", "echo hi; sleep 30 & exit 3", timeout=20)
    assert time.monotonic() - t0 < 8
    assert r.returncode == 3
    p = store.get_post(r.stdout.strip())
    assert p["payload"]["text"] == "hi\n" and p["live"] is False and p["payload"]["exit_code"] == 3


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])  # (sh background jobs ignore SIGINT)
def test_log_signal_reaches_whole_group_and_closes_post(board_env, store, tmp_path, sig):
    marker = tmp_path / "gc.pid"
    proc = spawn(board_env, "log", "-a", "t", "--", "sh", "-c",
                 f"echo start; sleep 60 & echo $! > {marker}; wait")
    pid = proc.stdout.readline().strip()
    wait_text(store, pid, "start")
    while not marker.exists() or not marker.read_text().strip():
        time.sleep(0.05)
    gc = int(marker.read_text())
    proc.send_signal(sig)
    proc.communicate(timeout=15)
    assert proc.returncode == 128 + sig
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(gc, 0)  # the grandchild got the signal too: no orphan
    p = store.get_post(pid)
    assert p["live"] is False and p["status"] == "error" and p["payload"]["exit_code"] == 128 + sig


def test_log_child_ignoring_sigterm_is_killed_after_grace(board_env, store):
    proc = spawn(board_env, "log", "-a", "t", "--", sys.executable, "-c",
                 "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); "
                 "time.sleep(60)")
    pid = proc.stdout.readline().strip()
    wait_text(store, pid, "up")
    t0 = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=20)
    assert 4 < time.monotonic() - t0 < 12
    assert proc.returncode == 128 + signal.SIGTERM
    assert store.get_post(pid)["live"] is False


def test_log_child_gets_exactly_one_sigint(board_env, store, tmp_path):
    count = tmp_path / "n"
    prog = (f"import signal,time,sys\nn=[0]\ndef h(s,f): n[0]+=1; open({str(count)!r},'w').write(str(n[0]))\n"
            "signal.signal(signal.SIGINT,h)\nprint('up',flush=True)\nt=time.time()\n"
            "while time.time()-t<3: time.sleep(0.05)\n")
    # board in its own session, like a terminal's foreground group: Ctrl-C signals the group
    proc = spawn(board_env, "log", "-a", "t", "--", sys.executable, "-c", prog, start_new_session=True)
    pid = proc.stdout.readline().strip()
    wait_text(store, pid, "up")
    os.killpg(proc.pid, signal.SIGINT)
    proc.communicate(timeout=20)
    assert count.read_text() == "1"  # forwarded once by board, not also delivered by the "tty"


def test_pipe_mode_sigterm_posts_pending_output_and_closes(board_env, store):
    proc = subprocess.Popen([*BOARD_ARGV, "log", "-a", "t"], env=board_env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pid = proc.stdout.readline().decode().strip()
    proc.stdin.write(b"line one\nline two\n")
    proc.stdin.flush()
    time.sleep(0.1)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=20)
    assert proc.returncode == 128 + signal.SIGTERM
    p = store.get_post(pid)
    assert p["payload"]["text"] == "line one\nline two\n"
    assert p["live"] is False and p["status"] == "error"


def test_append_stream_sigint_flushes(board_env, store, board):
    pid = board("post", "-a", "t", "--md", "start").stdout.strip()
    proc = subprocess.Popen([*BOARD_ARGV, "append", pid, "-"], env=board_env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc.stdin.write(b"more")
    proc.stdin.flush()
    time.sleep(0.3)
    proc.send_signal(signal.SIGINT)
    proc.communicate(timeout=20)
    assert proc.returncode == 130
    assert store.get_post(pid)["payload"]["markdown"] == "start\n\nmore"


def test_log_with_stdout_closed_still_runs_command(board_env, store, tmp_path):
    flag = tmp_path / "ran"
    r = subprocess.run(f"{sys.executable} -m board.cli log -a t -- touch {flag} >&-", shell=True, env=board_env,
                       capture_output=False, stderr=subprocess.PIPE, text=True, timeout=30)
    assert r.returncode == 0 and flag.exists() and "Traceback" not in r.stderr
    r = subprocess.run(f"{sys.executable} -m board.cli post -a t hi >&-", shell=True, env=board_env,
                       stderr=subprocess.PIPE, text=True, timeout=30)
    assert r.returncode == 0 and "Traceback" not in r.stderr


def test_signal_during_final_retry_keeps_child_exit_code(board_env, tmp_path):
    proc = spawn(board_env, "log", "-a", "t", "--", "sh", "-c", "echo hi; exit 0",
                 env={"BOARD_URL": "http://127.0.0.1:9", "BOARD_FINISH_PATIENCE": "60"})
    deadline = time.time() + 20
    err = ""
    while time.time() < deadline:  # wait for the final-retry warning
        line = proc.stderr.readline()
        err += line
        if "retrying for up to" in line:
            break
    proc.send_signal(signal.SIGINT)
    _, rest = proc.communicate(timeout=20)
    assert proc.returncode == 0, err + rest
    assert "giving up" in rest or "not posted" in rest


def test_log_runs_child_when_store_cannot_be_opened(board_env, tmp_path):
    bad = tmp_path / "file"
    bad.write_text("not a dir")
    flag = tmp_path / "ran"
    r = run_board(board_env, "log", "-a", "t", "--", "sh", "-c", f"echo out; touch {flag}; exit 5",
                  env={"BOARD_DIR": str(bad)})
    assert r.returncode == 5 and flag.exists()
    assert "out" in r.stderr and "without posting" in r.stderr and "Traceback" not in r.stderr
    r = run_board(board_env, "log", "-a", "t", input="piped\n", env={"BOARD_DIR": str(bad)})
    assert "piped" in r.stderr and r.returncode in (1, 2) and "Traceback" not in r.stderr
