"""Regressions for the CLI/docs, crash-recovery and termination QA passes (batches 2-4)."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from board import store as store_mod
from board.cli import is_transient
from board.server import create_app
from board.store import BoardError, Store, StoreBusy

BOARD_ARGV = [sys.executable, "-m", "board.cli"]
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 17


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "srv")) as c:
        yield c


def clean_fail(r, code):
    assert r.returncode == code, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr and r.stderr.startswith("board: "), r.stderr


# ---------------------------------------------------------------- A. NaN / Infinity


def test_nan_rejected_everywhere(board, store, client):
    clean_fail(board("post", "-a", "x", "--chart", '{"series":[{"data":[NaN,1]}]}'), 2)
    clean_fail(board("post", "-a", "x", "--table", '[{"a": Infinity}]'), 2)
    assert store.list_posts()[0] == []
    r = client.post("/api/posts", content=b'{"agent":"x","kind":"chart","payload":{"option":{"series":[NaN]}}}',
                    headers={"content-type": "application/json"})
    assert r.status_code == 400
    with pytest.raises(BoardError):
        store.create_post("x", "chart", {"option": {"series": [{"data": [float("nan")]}]}})
    assert client.get("/api/posts").status_code == 200


# ---------------------------------------------------------------- B. wait never hangs on permanent errors


def test_wait_bad_id_and_permanent_errors_exit_fast(board):
    t0 = time.monotonic()
    clean_fail(board("wait", "p\udcff"), 2)
    clean_fail(board("wait", "nonsense"), 2)
    clean_fail(board("wait", "p_zzzzzz"), 1)
    assert time.monotonic() - t0 < 15


# ---------------------------------------------------------------- C. invalid UTF-8


def test_invalid_utf8_argv_and_api_surrogates(board_env, store, client):
    args = [*BOARD_ARGV, "post", "-a", "x", "-t", b"t\xff".decode("utf-8", "surrogateescape"),
            b"text \xff\xfe".decode("utf-8", "surrogateescape")]
    r = subprocess.run(args, env=board_env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    p = store.get_post(r.stdout.strip())
    assert p["payload"]["text"] == "text ��" and p["title"] == "t�"
    r = client.post("/api/posts", content=b'{"agent":"x","kind":"text","payload":{"text":"\\ud800"}}',
                    headers={"content-type": "application/json"})
    assert r.status_code == 400 and "UTF-8" in r.json()["error"]


# ---------------------------------------------------------------- D. deep nesting


def test_deeply_nested_json(board, client):
    deep = "[" * 100_000 + "]" * 100_000
    clean_fail(board("post", "-a", "x", "--chart", deep), 2)
    clean_fail(board("post", "-a", "x", "--table", deep), 2)
    r = client.post("/api/posts", content=('{"agent":"x","kind":"chart","payload":' + deep + "}").encode(),
                    headers={"content-type": "application/json"})
    assert r.status_code == 400


# ---------------------------------------------------------------- E/F. table shapes, CSV


def test_table_shapes_validated_and_csv_edge_cases(board, store, client):
    for bad in ('{"columns":5,"rows":[[1]]}', '{"columns":["a"],"rows":[1,2]}', '{"columns":["a"],"rows":"ab"}',
                '{"columns":null,"rows":[]}'):
        clean_fail(board("post", "-a", "x", "--table", bad), 2)
    r = client.post("/api/posts", json={"agent": "x", "kind": "table", "payload": {"columns": 5, "rows": [1]}})
    assert r.status_code == 400
    pid = board("post", "-a", "x", "--table", "a,b\r1,2").stdout.strip()
    assert store.get_post(pid)["payload"] == {"columns": ["a", "b"], "rows": [["1", "2"]]}
    pid = board("post", "-a", "x", "--table", "﻿name,n\nx,1\n").stdout.strip()
    assert store.get_post(pid)["payload"]["columns"] == ["name", "n"]


# ---------------------------------------------------------------- G/Q. keys and titles


def test_key_limits_and_title_warning(board, store, client):
    clean_fail(board("post", "-a", "x", "-k", "k" * 101, "hi"), 2)
    clean_fail(board("post", "-a", "x", "-k", "   ", "hi"), 2)
    assert client.post("/api/posts", json={"agent": "x", "kind": "text", "payload": {"text": "a"},
                                           "key": "k" * 101}).status_code == 400
    r = board("post", "-a", "x", "-t", "T" * 250, "hi")
    assert r.returncode == 0 and "title is longer than 200" in r.stderr
    assert len(store.get_post(r.stdout.strip())["title"]) == 200


def test_ls_agent_filter_trailing_slash(board):
    board("post", "-a", "lead/tests", "hi")
    assert "lead/tests" in board("ls", "-a", "lead/").stdout
    clean_fail(board("ls", "-a", "Bad Name"), 2)


# ---------------------------------------------------------------- H. @text


def test_at_text_literal_unless_path_like(board, store):
    for t in ("@alice.smith", "@v1.2", "@human please"):
        r = board("post", "-a", "x", t)
        assert r.returncode == 0, r.stderr
        assert store.get_post(r.stdout.strip())["payload"]["text"] == t
    r = board("post", "-a", "x", "@bob/charlie")
    clean_fail(r, 2)
    assert "file not found: bob/charlie" in r.stderr and "pipe it in with -" in r.stderr
    clean_fail(board("post", "-a", "x", "--md", "@report.md"), 2)


# ---------------------------------------------------------------- I. -h as an option value


def test_dash_h_as_option_value_is_not_help(board, store):
    r = board("post", "-a", "x", "-t", "-h", "text")
    assert r.returncode == 0 and r.stdout.startswith("p_"), r.stdout
    assert store.get_post(r.stdout.strip())["title"] == "-h"
    r = board("log", "-a", "x", "-t", "-h", "--", "echo", "hi")
    assert r.returncode == 0 and store.get_post(r.stdout.strip())["title"] == "-h"
    assert board("post", "-a", "x", "-h").stdout.startswith("board:")  # standalone -h is still help


# ---------------------------------------------------------------- J/9. environment errors


def test_environment_errors_are_one_line(board, tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    clean_fail(board("post", "-a", "x", "hi", env={"BOARD_DIR": str(f)}), 2)
    d = tmp_path / "notboard"
    d.mkdir()
    (d / "board.db").write_bytes(b"this is not sqlite" * 100)
    r = board("post", "-a", "x", "hi", env={"BOARD_DIR": str(d)})
    clean_fail(r, 1)
    assert "not a board database" in r.stderr
    e = tmp_path / "other"
    e.mkdir()
    db = sqlite3.connect(e / "board.db")
    db.execute("CREATE TABLE users(id)")
    db.commit()
    db.close()
    r = board("post", "-a", "x", "hi", env={"BOARD_DIR": str(e)})
    clean_fail(r, 1)
    assert "not a board database" in r.stderr
    clean_fail(board("post", "-a", "x", "hi", env={"BOARD_DIR": str(tmp_path / ("n" * 300) / "x")}), 2)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_readonly_db_is_clean_error_and_log_still_runs(board, store, board_dir, tmp_path):
    store.create_post("x", "text", {"text": "a"})
    store.close()
    for p in board_dir.iterdir():
        if p.is_file():
            p.chmod(0o444)
    board_dir.chmod(0o555)
    try:
        clean_fail(board("post", "-a", "x", "hi"), 1)
        flag = tmp_path / "ran"
        r = board("log", "-a", "x", "--", "sh", "-c", f"touch {flag}; exit 7")
        assert r.returncode == 7 and flag.exists() and "Traceback" not in r.stderr
    finally:
        board_dir.chmod(0o755)
        for p in board_dir.iterdir():
            if p.is_file():
                p.chmod(0o644)


# ---------------------------------------------------------------- K/P. size parity


def test_direct_mode_size_caps(board, tmp_path):
    big = tmp_path / "big.md"
    big.write_text("x" * (9 * 1024 * 1024))
    r = board("post", "-a", "x", "--md", f"@{big}")
    clean_fail(r, 1)
    assert "larger than 8 MB" in r.stderr
    rows = tmp_path / "rows.csv"
    rows.write_text("a\n" + "1\n" * 50_001)
    clean_fail(board("post", "-a", "x", "--table", f"@{rows}"), 1)


# ---------------------------------------------------------------- L. -q on ask / wait


def test_ask_and_wait_accept_quiet(board, store):
    r = board("ask", "-a", "x", "-q", "--timeout", "0", "ok?")
    assert r.returncode == 4
    pid = r.stderr.split(" on ")[1].split()[0]
    store.reply(pid, "approve")
    r = board("wait", "-q", pid)
    assert r.returncode == 0 and r.stdout == ""


# ---------------------------------------------------------------- M. re-blocking clears the reply


def test_reblock_clears_reply(board, store):
    p, _ = store.create_post("x", "text", {"text": "q", "ask": True}, status="blocked")
    store.reply(p["id"], "approve")
    assert board("append", p["id"], "-s", "blocked").returncode == 0
    q = store.get_post(p["id"])
    assert "reply" not in q["payload"] and q["status"] == "blocked" and q["gen"] != p["gen"]
    assert store.reply(p["id"], "reject")["payload"]["reply"]["action"] == "reject"


# ---------------------------------------------------------------- N/O. API types, since range


def test_api_types_and_since_range(client):
    r = client.post("/api/posts", json={"agent": "x", "kind": "log", "payload": {}, "live": "false"})
    assert r.status_code == 400
    r = client.post("/api/posts", json={"agent": "x", "kind": "log", "payload": {"exit_code": "x"}})
    assert r.status_code == 400
    p = client.post("/api/posts", json={"agent": "x", "kind": "log", "payload": {}, "live": True}).json()
    assert client.post(f"/api/posts/{p['id']}/append", json={"done": "false"}).status_code == 400
    with client.stream("GET", "/api/events?since=-99999999999999999999") as s:
        lines = []
        for line in s.iter_lines():
            lines.append(line)
            if line.startswith("event: reset"):
                break
        assert "event: reset" in lines


# ---------------------------------------------------------------- R. truncated images


def test_header_only_png_rejected(board, tmp_path):
    f = tmp_path / "x.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    r = board("post", "-a", "x", "--image", str(f))
    clean_fail(r, 1)
    assert "not an image" in r.stderr


# ---------------------------------------------------------------- batch 3


def test_server_refuses_writes_after_data_dir_removed(tmp_path):
    import shutil
    d = tmp_path / "srv"
    with TestClient(create_app(d)) as c:
        assert c.post("/api/posts", json={"agent": "x", "kind": "text", "payload": {"text": "a"}}).status_code == 201
        shutil.rmtree(d)
        r = c.post("/api/posts", json={"agent": "x", "kind": "text", "payload": {"text": "b"}})
        assert r.status_code == 503 and "removed or replaced" in r.json()["error"]


def test_lock_wait_has_one_overall_deadline(board, board_dir, store, tmp_path):
    holder = sqlite3.connect(board_dir / "board.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        t0 = time.monotonic()
        r = board("post", "-a", "x", "hi", env={"BOARD_LOCK_PATIENCE": "1"})
        assert time.monotonic() - t0 < 10
        clean_fail(r, 1)
        assert "locked by another process" in r.stderr
        flag = tmp_path / "ran"
        r = board("log", "-a", "x", "--", "sh", "-c", f"touch {flag}; exit 6",
                  env={"BOARD_LOCK_PATIENCE": "1", "BOARD_FINISH_PATIENCE": "1"})
        assert r.returncode == 6 and flag.exists()
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_tx_keeps_the_original_error(store):
    def boom(c):
        c.execute("ROLLBACK")  # as SQLite does itself on disk full / I/O errors
        raise sqlite3.OperationalError("database or disk is full")

    with pytest.raises(sqlite3.OperationalError, match="disk is full"):
        store._tx(boom)
    assert not is_transient(sqlite3.OperationalError("database or disk is full"))
    assert is_transient(sqlite3.OperationalError("database is locked"))
    assert is_transient(StoreBusy("locked"))


def test_post_id_collision_retries(store, monkeypatch):
    p, _ = store.create_post("x", "text", {"text": "a"})
    ids = iter([p["id"], p["id"], "p_newnewnn"])
    monkeypatch.setattr(store_mod, "new_id", lambda prefix, n: next(ids))
    q, _ = store.create_post("x", "text", {"text": "b"})
    assert q["id"] == "p_newnewnn"


def test_events_pruned_and_small_request_results(store, monkeypatch):
    monkeypatch.setattr(store_mod, "EVENT_KEEP", 100)
    p, _ = store.create_post("x", "log", {}, live=True, request_id="big")
    for i in range(1100):
        store.append(p["id"], text=f"{i}\n")
    assert store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] < 1000
    assert store.min_event_id() > 1
    stored = json.loads(store.conn.execute("SELECT result FROM requests WHERE id='big'").fetchone()[0])
    assert stored["result"] == {"id": p["id"], "created": True}
    again, created = store.create_post("x", "log", {}, live=True, request_id="big")
    assert again["id"] == p["id"] and created and again["payload"]["text"].startswith("0\n")


def test_reaper_detects_pid_reuse(store):
    import socket
    p, _ = store.create_post("x", "log", {"pid": os.getpid(), "host": socket.gethostname(),
                                          "pstart": "Thu Jan  1 00:00:00 1970"}, live=True)
    assert store.reap_dead_logs() == 1
    assert store.get_post(p["id"])["live"] is False
    q, _ = store.create_post("x", "log", {"pid": os.getpid(), "host": "some-other-host"}, live=True)
    store.reap_dead_logs()
    assert store.get_post(q["id"])["live"] is True  # another machine: don't guess


def test_orphan_assets_swept(store):
    a = store.add_asset(PNG)
    b = store.add_asset(PNG + b"1")
    store.create_post("x", "image", {"asset_id": b["id"]})
    assert store.sweep_orphan_assets(older_than=3600) == 0
    assert store.sweep_orphan_assets(older_than=-1) == 1
    with pytest.raises(BoardError):
        store.get_asset(a["id"])
    store.get_asset(b["id"])


def test_lists_ordered_by_seq_not_clock(store):
    a, _ = store.create_post("x", "text", {"text": "a"})
    b, _ = store.create_post("x", "text", {"text": "b"})
    store.conn.execute("UPDATE posts SET updated_at='2999-01-01T00:00:00.000Z' WHERE id=?", (a["id"],))
    assert [p["id"] for p in store.list_posts()[0]] == [b["id"], a["id"]]


# ---------------------------------------------------------------- batch 4


def test_serve_sigterm_exits_cleanly(board_env, tmp_path):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    p = subprocess.Popen([*BOARD_ARGV, "serve", "--port", str(port)], env=board_env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    for _ in range(200):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            break
        except OSError:
            time.sleep(0.05)
    p.send_signal(signal.SIGTERM)
    _, err = p.communicate(timeout=15)
    assert p.returncode == 0 and "Traceback" not in err


def test_cli_reaps_dead_logs_without_server(board, store, board_dir):
    import socket
    dead = subprocess.Popen(["true"])
    dead.wait()
    p, _ = store.create_post("x", "log", {"pid": dead.pid, "host": socket.gethostname()}, live=True)
    assert board("ls").returncode == 0
    assert store.get_post(p["id"])["live"] is False
    assert (board_dir / ".reaped").exists()
