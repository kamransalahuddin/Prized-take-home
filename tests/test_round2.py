"""Regressions for the round-2 stress findings (CLI/docs, duplicates, termination and
crash-recovery QA passes). Test names carry the finding id."""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from board import cli as cli_mod
from board import store as store_mod
from board.server import create_app
from board.store import MAX_DEPTH, BoardError, Store

BOARD_ARGV = [sys.executable, "-m", "board.cli"]
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 17


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "srv")) as c:
        yield c


def clean_fail(r, code):
    assert r.returncode == code, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr and r.stderr.startswith("board: "), r.stderr


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int, timeout: float = 15) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"port {port} never opened")


def deep(n: int) -> str:
    return '{"series":' + "[" * n + "1" + "]" * n + "}"


# ---------------------------------------------------------------- A: depth limit


def test_A_deep_chart_rejected_at_write_time(board, client):
    clean_fail(board("post", "-a", "x", "--chart", deep(9986)), 2)
    clean_fail(board("post", "-a", "x", "--chart", deep(200)), 2)
    assert board("post", "-a", "x", "--chart", deep(MAX_DEPTH - 2)).returncode == 0
    r = client.post("/api/posts", json={"agent": "x", "kind": "chart", "payload": {"option": json.loads(deep(100))}})
    assert r.status_code == 400 and "nested too deeply" in r.json()["error"]
    body = '{"agent":"x","kind":"chart","payload":{"option":' + deep(5000) + "}}"
    r = client.post("/api/posts", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 400
    with pytest.raises(BoardError):
        Store(client.app.state.data_dir).create_post("x", "chart", {"option": json.loads(deep(100))})


def test_A_stored_deep_data_never_breaks_reads(tmp_path):
    d = tmp_path / "srv"
    st = Store(d)
    p, _ = st.create_post("x", "chart", {"option": {"series": []}})
    bad = '{"option":' + deep(5000) + "}"
    st.conn.execute("UPDATE posts SET payload=? WHERE id=?", (bad, p["id"]))
    st.conn.execute("UPDATE events SET data=? WHERE post_id=?",
                    (json.dumps({**p, "payload": None})[:-1].replace('"payload": null', '"payload": ' + bad) + "}",
                     p["id"]))
    st.close()
    with TestClient(create_app(d)) as c:
        r = c.get("/api/posts")
        assert r.status_code == 200 and "error" in r.json()["posts"][0]["payload"]
        assert c.get(f"/api/posts/{p['id']}").status_code == 200
    ev = Store(d).events_after(0)
    assert ev and "error" in ev[0]["data"]["payload"]
    json.dumps(ev)  # serializable


# ---------------------------------------------------------------- N1: replay must not adopt a newer post


def test_N1_create_replay_after_key_replace_is_409(store):
    a, _ = store.create_post("x", "log", {}, key="run", live=True, request_id="A")
    b, _ = store.create_post("x", "log", {}, key="run", live=True, request_id="B")
    assert a["id"] == b["id"] and a["gen"] != b["gen"]
    with pytest.raises(BoardError) as e:
        store.create_post("x", "log", {}, key="run", live=True, request_id="A")  # lost-response retry of A
    assert e.value.status == 409 and "replaced" in e.value.message
    again, _ = store.create_post("x", "log", {}, key="run", live=True, request_id="B")
    assert again["gen"] == b["gen"]


def test_N1_ask_replay_cannot_take_the_newer_question(client):
    body = {"agent": "x", "kind": "text", "payload": {"text": "Q1", "ask": True}, "status": "blocked",
            "key": "q", "request_id": "ask-A"}
    assert client.post("/api/posts", json=body).status_code == 201
    client.post("/api/posts", json=dict(body, payload={"text": "Q2", "ask": True}, request_id="ask-B"))
    r = client.post("/api/posts", json=body)
    assert r.status_code == 409 and "replaced" in r.json()["error"]


def test_N1_reply_replay_after_reopen_is_409(store):
    q, _ = store.create_post("x", "text", {"text": "q", "ask": True}, status="blocked")
    store.reply(q["id"], "approve", request_id="R")
    assert store.reply(q["id"], "approve", request_id="R")["payload"]["reply"]["action"] == "approve"
    store.append(q["id"], status="blocked")  # re-opened: reply cleared, gen bumped
    with pytest.raises(BoardError) as e:
        store.reply(q["id"], "approve", request_id="R")
    assert e.value.status == 409


def test_F_old_format_create_replay_gets_gen(store):
    p, _ = store.create_post("x", "text", {"text": "a"})
    old = dict(p)
    old.pop("gen")
    fp = store_mod.fingerprint("create_post", ["x", "text", {"text": "b"}, None, "info", None, False])
    store.conn.execute("INSERT INTO requests(id, result, created_at, fp) VALUES ('old', ?, ?, ?)",
                       (json.dumps({"op": "create_post", "result": [old, True]}), store_mod.now_iso(), fp))
    post, _ = store.create_post("x", "text", {"text": "b"}, request_id="old")
    assert post["gen"] == p["gen"]


# ---------------------------------------------------------------- B: overflow to Infinity


@pytest.mark.parametrize("flag,value", [("--chart", '{"series":[{"data":[1e999]}]}'),
                                        ("--table", '[["a"],[1e999]]')])
def test_B_overflowing_numbers_exit_2_in_both_modes(board, flag, value):
    clean_fail(board("post", "-a", "x", flag, value), 2)
    url = f"http://127.0.0.1:{free_port()}"
    r = board("post", "-a", "x", flag, value, env={"BOARD_URL": url})
    clean_fail(r, 2)
    assert "Infinity" in r.stderr


# ---------------------------------------------------------------- C: reaper and pid range


def test_C_pid_range_validated(store, client):
    for pid in (0, -1, 2**31, 2**40, True):
        with pytest.raises(BoardError):
            store.create_post("x", "log", {"pid": pid, "host": "h"}, live=True)
    r = client.post("/api/posts", json={"agent": "x", "kind": "log", "live": True,
                                        "payload": {"pid": 2**40, "host": "h"}})
    assert r.status_code == 400


def test_C_one_bad_row_does_not_stop_reaping(store):
    host = socket.gethostname()
    bad, _ = store.create_post("x", "log", {}, live=True)
    store.conn.execute("UPDATE posts SET payload=? WHERE id=?",
                       (json.dumps({"pid": 2**40, "host": host, "exit_code": None}), bad["id"]))
    weird, _ = store.create_post("x", "log", {}, live=True)
    store.conn.execute("UPDATE posts SET payload='[1]' WHERE id=?", (weird["id"],))
    dead = subprocess.Popen(["true"])
    dead.wait()
    good, _ = store.create_post("x", "log", {"pid": dead.pid, "host": host}, live=True)
    store.reap_dead_logs()
    assert store.get_post(good["id"])["live"] is False
    assert store.get_post(bad["id"])["live"] is False  # out of range: not a live process


# ---------------------------------------------------------------- D: patience env vars


def test_D_env_seconds_falls_back(monkeypatch, capsys):
    for raw in ("abc", "nan", "inf", "-1", "1e999"):
        monkeypatch.setenv("BOARD_X_PATIENCE", raw)
        assert store_mod.env_seconds("BOARD_X_PATIENCE", 7) == 7
        assert "ignoring BOARD_X_PATIENCE" in capsys.readouterr().err
    monkeypatch.setenv("BOARD_X_PATIENCE", "2.5")
    assert store_mod.env_seconds("BOARD_X_PATIENCE", 7) == 2.5


def test_D_bad_patience_env_is_not_a_traceback(board):
    env = {"BOARD_LOCK_PATIENCE": "abc", "BOARD_WRITE_PATIENCE": "nan", "BOARD_FINISH_PATIENCE": "-3",
           "BOARD_SERVER_ERROR_PATIENCE": "inf"}
    r = board("post", "-a", "x", "hi", env=env)
    assert r.returncode == 0 and "Traceback" not in r.stderr
    assert r.stderr.count("ignoring BOARD_") == 4


# ---------------------------------------------------------------- N2: sent but unanswered


class SilentServer:
    """Accepts connections and reads requests but never answers."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.conns = []
        self.requests = 0
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(c)
            self.requests += 1

    def close(self):
        self.sock.close()
        for c in self.conns:
            c.close()


def test_N2_unanswered_write_is_maybe_applied_not_unreachable(monkeypatch, tmp_path, capsys):
    srv = SilentServer()
    monkeypatch.setattr(cli_mod, "READ_TIMEOUT", 0.3)
    monkeypatch.setattr(cli_mod, "WRITE_PATIENCE", 0.5)
    monkeypatch.setenv("BOARD_URL", f"http://127.0.0.1:{srv.port}")
    try:
        code = cli_mod.run(["post", "-a", "x", "hello"])
    finally:
        srv.close()
    err = capsys.readouterr().err
    assert code == 1, err
    assert "may already have been applied (request id " in err and "cannot reach" not in err
    assert srv.requests >= 2  # retried (same request id)


def test_N2_connect_failure_is_still_exit_3(monkeypatch, capsys):
    monkeypatch.setattr(cli_mod, "WRITE_PATIENCE", 0.2)
    monkeypatch.setenv("BOARD_URL", f"http://127.0.0.1:{free_port()}")
    assert cli_mod.run(["post", "-a", "x", "hello"]) == 3
    assert "cannot reach BOARD_URL" in capsys.readouterr().err


def test_N2_timeouts_split_connect_and_read():
    be = cli_mod.HttpBackend("http://127.0.0.1:1")
    assert be.client.timeout.connect == cli_mod.CONNECT_TIMEOUT <= 5
    assert be.client.timeout.read == cli_mod.READ_TIMEOUT >= 20


def test_E_ls_gives_up_sooner_than_writes(board):
    t = time.time()
    r = board("ls", env={"BOARD_URL": f"http://127.0.0.1:{free_port()}"})
    clean_fail(r, 3)
    assert time.time() - t < 12


# ---------------------------------------------------------------- P2-1: orphans after Ctrl-C


def test_P2_1_stop_signal_ends_background_jobs_of_the_group(board_env, tmp_path):
    pidfile = tmp_path / "bg.pid"
    p = subprocess.Popen([*BOARD_ARGV, "log", "-a", "x", "--", "sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait"],
                         env=board_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for _ in range(200):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        time.sleep(0.05)
    bg = int(pidfile.read_text())
    p.send_signal(signal.SIGINT)
    p.communicate(timeout=20)
    assert p.returncode > 128  # (macOS sh exits 129 when its wait is interrupted)
    for _ in range(40):
        try:
            os.kill(bg, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(bg, signal.SIGKILL)
        raise AssertionError("background job survived the stop signal")


def test_P2_1_without_signal_background_jobs_are_left_alone(board, tmp_path):
    pidfile = tmp_path / "bg.pid"
    r = board("log", "-a", "x", "-q", "--", "sh", "-c", f"sleep 30 >/dev/null 2>&1 & echo $! > {pidfile}")
    assert r.returncode == 0
    bg = int(pidfile.read_text())
    try:
        os.kill(bg, 0)  # still running
    finally:
        os.kill(bg, signal.SIGKILL)


# ---------------------------------------------------------------- P2-2 / P2-4 / M: serve


def test_P2_2_serve_with_stdout_closed(board_env, tmp_path):
    port = free_port()
    p = subprocess.Popen(["sh", "-c", f'exec "$0" -m board.cli serve --port {port} >&-', sys.executable],
                         env=board_env, stderr=subprocess.PIPE, text=True)
    try:
        wait_port(port)
        import httpx
        assert httpx.get(f"http://127.0.0.1:{port}/api/posts", timeout=5).status_code == 200
    finally:
        p.send_signal(signal.SIGTERM)
        _, err = p.communicate(timeout=15)
    assert p.returncode == 0 and "Traceback" not in err


@pytest.mark.parametrize("sig", [signal.SIGHUP, signal.SIGQUIT])
def test_P2_4_serve_hup_and_quit_shut_down_cleanly(board_env, sig):
    port = free_port()
    p = subprocess.Popen([*BOARD_ARGV, "serve", "--port", str(port)], env=board_env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    wait_port(port)
    p.send_signal(sig)
    _, err = p.communicate(timeout=15)
    assert p.returncode == 0 and "Traceback" not in err


def test_M_serve_bad_host_names_the_cause(board):
    r = board("serve", "--host", "bad host", "--port", str(free_port()))
    clean_fail(r, 1)
    assert "unknown host name" in r.stderr and "Another program" not in r.stderr


# ---------------------------------------------------------------- P2-3: no stdin / editor


def test_P2_3_git_env_in_child_and_help(board, store):
    r = board("log", "-a", "x", "--", "sh", "-c", 'echo "E=$GIT_EDITOR P=$GIT_TERMINAL_PROMPT"')
    pid = r.stdout.strip()
    assert "E=true P=0" in store.get_post(pid)["payload"]["text"]
    r = board("log", "-a", "x", "--", "sh", "-c", 'echo "E=$GIT_EDITOR"', env={"GIT_EDITOR": "vim"})
    assert "E=vim" in store.get_post(r.stdout.strip())["payload"]["text"]
    from board.help import HELP
    assert "no" in HELP and "GIT_EDITOR=true" in HELP


# ---------------------------------------------------------------- P2-5: handlers kept


def test_P2_5_handlers_stay_installed_in_the_cli_process(monkeypatch):
    monkeypatch.setattr(cli_mod, "KEEP_SIGNAL_HANDLERS", True)
    before = signal.getsignal(signal.SIGTERM)
    try:
        with cli_mod.StopSignals(lambda s: None) as sigs:
            pass
        assert signal.getsignal(signal.SIGTERM) == sigs._handler
    finally:
        signal.signal(signal.SIGTERM, before)
        for sig, h in sigs.old.items():
            signal.signal(sig, h)


# ---------------------------------------------------------------- P2-7 / C: asset sweep


def test_P2_7_sweep_removes_unreferenced_files(store):
    a = store.add_asset(PNG)
    stray = store.assets_dir / "a_stray123.png"
    stray.write_bytes(PNG)
    old = time.time() - 7200
    os.utime(stray, (old, old))
    fresh = store.assets_dir / "a_fresh123.png"
    fresh.write_bytes(PNG)
    store.sweep_orphan_assets(older_than=3600)
    assert not stray.exists() and fresh.exists()
    path, _ = store.get_asset(a["id"])
    assert path.exists()  # referenced by a row, recent: kept
    store.sweep_orphan_assets(older_than=-1)
    assert not path.exists()
    with pytest.raises(BoardError):
        store.get_asset(a["id"])


def test_F_failed_asset_write_leaves_no_tmp(store, monkeypatch):
    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store_mod.os, "fsync", boom)
    with pytest.raises(OSError):
        store.add_asset(PNG)
    assert not list(store.assets_dir.glob(".*.tmp"))


# ---------------------------------------------------------------- P2-8: streamed append separators


def test_P2_8_streamed_append_to_text_has_one_separator(board_env, store):
    p, _ = store.create_post("x", "text", {"text": "start"})
    producer = "printf 'a'; sleep 0.6; printf 'b\\n'; sleep 0.6; printf 'c\\n'"
    r = subprocess.run(["sh", "-c", f"({producer}) | {sys.executable} -m board.cli append {p['id']} - -q"],
                       env=board_env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert store.get_post(p["id"])["payload"]["text"] == "start\nab\nc\n"


def test_P2_8_raw_append_store_and_api(store, client):
    p, _ = store.create_post("x", "markdown", {"markdown": "# h"})
    store.append(p["id"], text="one ")
    store.append(p["id"], text="two", raw=True)
    assert store.get_post(p["id"])["payload"]["markdown"] == "# h\n\none two"
    q = client.post("/api/posts", json={"agent": "x", "kind": "text", "payload": {"text": "s"}}).json()
    client.post(f"/api/posts/{q['id']}/append", json={"text": "1"})
    client.post(f"/api/posts/{q['id']}/append", json={"text": "2", "raw": True})
    assert client.get(f"/api/posts/{q['id']}").json()["payload"]["text"] == "s\n12"


# ---------------------------------------------------------------- F..L small validation


def test_F_help_and_id_message_use_8_char_ids(board):
    from board.help import HELP
    assert "p_3f9k2a7q" in HELP and "p_3f9k2a " not in HELP and "p_3f9k2a)" not in HELP
    r = board("append", "nope", "x")
    clean_fail(r, 2)
    assert "p_3f9k2a7q" in r.stderr


@pytest.mark.parametrize("value", ["[[]]", "[{}]", '[["a"]]', '{"columns": [], "rows": []}', '{"columns": ["a"], "rows": []}'])
def test_G_json_tables_need_header_and_rows(board, client, value):
    clean_fail(board("post", "-a", "x", "--table", value), 2)
    payload = json.loads(value)
    payload = payload if isinstance(payload, dict) else {"table": payload}
    assert client.post("/api/posts", json={"agent": "x", "kind": "table", "payload": payload}).status_code == 400


def test_H_empty_text_rejected(board, client):
    for v in ("", "   ", "\n\t"):
        clean_fail(board("post", "-a", "x", v), 2)
        clean_fail(board("post", "-a", "x", "--md", v), 2)
    assert client.post("/api/posts", json={"agent": "x", "kind": "text", "payload": {"text": " "}}).status_code == 400


def test_I_chart_series_live_and_ask_types(board, client):
    clean_fail(board("post", "-a", "x", "--chart", '{"series": null}'), 2)
    clean_fail(board("post", "-a", "x", "--chart", '{"series": 3}'), 2)
    assert board("post", "-a", "x", "--chart", '{"series": {"type": "line", "data": [1]}}').returncode == 0
    post = lambda **kw: client.post("/api/posts", json={"agent": "x", **kw})  # noqa: E731
    assert post(kind="chart", payload={"option": {"series": None}}).status_code == 400
    r = post(kind="text", payload={"text": "a"}, live=True)
    assert r.status_code == 400 and "log" in r.json()["error"]
    assert post(kind="text", payload={"text": "a", "ask": "yes"}).status_code == 400
    assert post(kind="text", payload={"text": "a", "ask": True}, status="blocked").status_code == 201


def test_J_limit_validation(board, client):
    for n in ("0", "-5"):
        clean_fail(board("ls", "-n", n), 2)
    for bad in ("1_0", "１", "-1", " 1", "1.5"):
        assert client.get("/api/posts", params={"limit": bad}).status_code == 400
    assert client.get("/api/posts", params={"limit": "10"}).status_code == 200


def test_K_ls_slash_is_bad_agent_name(board):
    r = board("ls", "-a", "/")
    clean_fail(r, 2)
    assert "bad agent name" in r.stderr


def test_agent_name_length_message(board):
    r = board("post", "-a", "a" * 65, "hi")
    clean_fail(r, 2)
    assert "at most 64 characters" in r.stderr


def test_L_expect_kind_message_without_replace(store):
    p, _ = store.create_post("x", "text", {"text": "a"})
    with pytest.raises(BoardError) as e:
        store.append(p["id"], text="b", expect_kind="log")
    assert "replaced" not in e.value.message and "text post" in e.value.message
    q, _ = store.create_post("x", "text", {"text": "a"}, key="k")
    store.create_post("x", "log", {}, key="k")
    with pytest.raises(BoardError) as e:
        store.append(q["id"], text="b", expect_kind="text", expect_gen=q["gen"])
    assert "replaced" in e.value.message


# ---------------------------------------------------------------- small gaps


def test_small_at_with_space_before_path(board, store, tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("from file")
    r = board("post", "-a", "x", "--md", f"@ {f}")
    assert r.returncode == 0, r.stderr
    assert store.get_post(r.stdout.strip())["payload"]["markdown"] == "from file"
    clean_fail(board("post", "-a", "x", f"@ {tmp_path}/missing.md"), 2)
    r = board("post", "-a", "x", "@ human please look")
    assert store.get_post(r.stdout.strip())["payload"]["text"] == "@ human please look"


def test_small_ls_on_read_only_db(board, store, board_dir):
    store.create_post("x", "text", {"text": "hello"})
    store.close()
    files = [p for p in board_dir.iterdir() if p.is_file()]
    for p in files:
        p.chmod(0o444)
    board_dir.chmod(0o555)
    try:
        r = board("ls")
        assert r.returncode == 0, r.stderr
        assert "hello" in r.stdout
        clean_fail(board("post", "-a", "x", "hi"), 1)
    finally:
        board_dir.chmod(0o755)
        for p in files:
            p.chmod(0o644)


def test_small_current_schema_opens_without_writing(board_dir):
    Store(board_dir).close()
    db = board_dir / "board.db"
    db.chmod(0o444)
    try:
        st = Store(board_dir)  # no schema writes on an up-to-date database
        assert st.list_posts()[0] == []
        st.close()
    finally:
        db.chmod(0o644)


def test_fingerprint_normalizes_title_and_expect_gen(store):
    p, _ = store.create_post("x", "log", {}, live=True)
    store.append(p["id"], text="a", title=" T ", request_id="r1")
    store.append(p["id"], text="a", title="T", request_id="r1")  # same request: no 409
    store.append(p["id"], text="b", expect_gen="", request_id="r2")
    store.append(p["id"], text="b", expect_gen=None, request_id="r2")
    assert store.get_post(p["id"])["payload"]["text"] == "ab"


# ---------------------------------------------------------------- crash-recovery batch


def test_B2_events_bounded_by_bytes(store, monkeypatch):
    monkeypatch.setattr(store_mod, "EVENT_BYTES", 1_000_000)
    p, _ = store.create_post("x", "log", {}, live=True)
    for _ in range(40):
        store.append(p["id"], text="x" * 100_000)
    total = store.conn.execute("SELECT SUM(size) FROM events").fetchone()[0]
    assert total <= 1_000_000 + 110_000
    assert store.min_event_id() > 1
    assert cli_mod.MAX_CHUNK <= 256 * 1024


def test_B2_old_database_gets_event_sizes(board_dir):
    st = Store(board_dir)
    st.create_post("x", "text", {"text": "a"})
    st.conn.execute("DROP INDEX events_size")
    st.conn.execute("ALTER TABLE events DROP COLUMN size")
    st.conn.execute("PRAGMA user_version=2")
    st.close()
    st = Store(board_dir)
    assert st.conn.execute("SELECT size FROM events").fetchone()[0] > 0
    st.close()


def test_A2_locked_db_does_not_delay_the_command(board_env, board_dir, tmp_path):
    Store(board_dir).close()
    holder = sqlite3.connect(board_dir / "board.db", isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    flag = tmp_path / "started"
    env = dict(board_env, BOARD_LOCK_PATIENCE="8")
    t = time.time()
    p = subprocess.Popen([*BOARD_ARGV, "log", "-a", "x", "-q", "--", "touch", str(flag)], env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        while not flag.exists() and time.time() - t < 6:
            time.sleep(0.05)
        started = time.time() - t
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    p.communicate(timeout=60)
    assert flag.exists() and started < 5, started


def test_F2_pipe_log_exit_1_when_posting_stopped(board_env, store):
    p, _ = store.create_post("x", "log", {}, key="k", live=True)
    # A pipe-mode log whose post gets replaced mid-stream (409): posting stops for good.
    script = (f"echo first; sleep 1; {sys.executable} -c \"from board.store import Store; "
              f"Store().create_post('x', 'text', {{'text': 'new'}}, key='k')\"; sleep 0.5; echo more")
    r = subprocess.run(["sh", "-c", f"({script}) | {sys.executable} -m board.cli log -a x -k k -q"],
                       env=board_env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1, r.stderr
    assert "stopped posting" in r.stderr


# ---------------------------------------------------------------- network batch (F1-F5)


def test_F1_pruning_during_slow_replay_sends_reset(tmp_path, monkeypatch):
    from test_sse_robustness import Live, read_events

    from board import server as server_mod
    monkeypatch.setattr(server_mod, "REPLAY_BATCH", 5)
    d = tmp_path / "data"
    st = Store(d)
    for i in range(30):
        st.create_post("x", "text", {"text": f"t{i}"})
    live = Live(d)
    reader = live.app.state.reader.store
    orig = reader.events_after
    calls = []

    def pruned_after_first_batch(seq, *a, **k):
        calls.append(seq)
        if len(calls) == 2:
            st.conn.execute("DELETE FROM events WHERE id <= 20")  # pruning overtakes the replay
        return orig(seq, *a, **k)

    reader.events_after = pruned_after_first_batch
    try:
        epoch = reader.epoch
        evs = read_events(live.url + "/api/events", params={"since": "0"},
                          until=lambda e: e["event"] == "reset", n=40)
        names = [e["event"] for e in evs]
        assert names == ["hello"] + ["post"] * 5 + ["reset"], names
        assert evs[-1]["id"] == f"{epoch}:30" and evs[-1]["data"]["seq"] == 30  # F5: reset has an id
    finally:
        live.stop()
        st.close()


def test_F2_watcher_behind_pruning_ends_streams_into_reset(tmp_path):
    from test_sse_robustness import Live, read_events

    d = tmp_path / "data"
    st = Store(d)
    st.create_post("x", "text", {"text": "first"})
    live = Live(d)
    hub = live.app.state.hub
    orig = hub.watch.events_after
    done = []

    def stalled(seq, *a, **k):  # as if the server was stopped while events were pruned
        if not done and st.current_seq() > seq + 2:
            st.conn.execute("DELETE FROM events WHERE id <= ?", (seq + 2,))
            done.append(seq)
        return orig(seq, *a, **k)

    hub.watch.events_after = stalled
    try:
        got, ended = [], threading.Event()

        def client():
            got.extend(read_events(live.url + "/api/events", n=100, timeout=10))
            ended.set()

        t = threading.Thread(target=client, daemon=True)
        t.start()
        time.sleep(0.5)
        for i in range(5):
            st.create_post("x", "text", {"text": f"n{i}"})
        assert ended.wait(8), "stream was not ended after the gap"
        assert [e["event"] for e in got] == ["hello"]  # nothing after the gap was sent silently
        evs = read_events(live.url + "/api/events", headers={"Last-Event-ID": f"{st.epoch}:1"}, n=2)
        assert [e["event"] for e in evs] == ["hello", "reset"]
    finally:
        live.stop()
        st.close()


def test_F3_stopped_server_is_maybe_applied_not_unreachable(board_env, monkeypatch, capsys):
    port = free_port()
    p = subprocess.Popen([*BOARD_ARGV, "serve", "--port", str(port)], env=board_env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_port(port)
        p.send_signal(signal.SIGSTOP)
        monkeypatch.setattr(cli_mod, "READ_TIMEOUT", 0.5)
        monkeypatch.setattr(cli_mod, "WRITE_PATIENCE", 0.5)
        monkeypatch.setenv("BOARD_URL", f"http://127.0.0.1:{port}")
        assert cli_mod.run(["post", "-a", "x", "hello"]) == 1
        err = capsys.readouterr().err
        assert "may already have been applied" in err and "cannot reach" not in err
    finally:
        p.send_signal(signal.SIGCONT)
        p.terminate()
        p.wait(15)


def test_F4_serve_uses_a_large_listen_backlog():
    import inspect
    assert "backlog=2048" in inspect.getsource(cli_mod.serve_cmd)
