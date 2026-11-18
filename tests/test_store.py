import os
"""Store-level sanity checks (board/store.py is shared by the CLI and the server)."""

import multiprocessing as mp

import pytest

from board.store import LOG_CAP, BoardError, Store, normalize_table, sniff_image, strip_ansi


def test_create_and_get(store):
    post, created = store.create_post("Lead", "text", {"text": "hi"}, title=" T ", status="OK")
    assert created and post["agent"] == "lead" and post["title"] == "T" and post["status"] == "ok"
    assert store.get_post(post["id"]) == post
    assert post["seq"] == store.current_seq()


def test_key_replace_keeps_id_and_clears_log(store):
    a, _ = store.create_post("lead", "log", {}, key="k", live=True)
    store.append(a["id"], "old\n")
    b, created = store.create_post("lead", "log", {}, key="k", live=True)
    assert not created and b["id"] == a["id"] and b["created_at"] == a["created_at"]
    assert b["payload"]["text"] == ""
    c, created = store.create_post("other", "log", {}, key="k")
    assert created and c["id"] != a["id"]  # keys are per agent


def test_append_semantics(store):
    t, _ = store.create_post("lead", "text", {"text": "a"})
    m, _ = store.create_post("lead", "markdown", {"markdown": "# a"})
    lg, _ = store.create_post("lead", "log", {}, live=True)
    store.append(t["id"], "b")
    store.append(m["id"], "b")
    store.append(lg["id"], "x", newline=True)
    store.append(lg["id"], "y")
    assert store.get_post(t["id"])["payload"]["text"] == "a\nb"
    assert store.get_post(m["id"])["payload"]["markdown"] == "# a\n\nb"
    assert store.get_post(lg["id"])["payload"]["text"] == "x\ny"
    res = store.append(lg["id"], done=True, exit_code=1)
    assert res["status"] == "error" and res["live"] is False
    tb, _ = store.create_post("lead", "table", {"columns": ["a"], "rows": [[1]]})
    with pytest.raises(BoardError) as e:
        store.append(tb["id"], "x")
    assert e.value.status == 409
    with pytest.raises(BoardError) as e:
        store.append("p_nope", "x")
    assert e.value.status == 404
    store.append(tb["id"], status="warn")  # status-only change works on any kind
    assert store.get_post(tb["id"])["status"] == "warn"


def test_append_events(store):
    lg, _ = store.create_post("lead", "log", {}, live=True)
    store.append(lg["id"], "chunk")
    store.append(lg["id"], status="warn")
    evs = store.events_after(lg["seq"])
    assert [e["type"] for e in evs] == ["append", "post"]
    assert evs[0]["data"]["text"] == "chunk" and evs[0]["data"]["seq"] == evs[0]["id"]
    assert evs[1]["data"]["status"] == "warn"


def test_log_cap_truncates(store):
    lg, _ = store.create_post("lead", "log", {}, live=True)
    chunk = "x" * (512 * 1024)
    for _ in range(6):
        store.append(lg["id"], chunk)
    p = store.get_post(lg["id"])
    assert p["payload"]["truncated"] is True
    assert len(p["payload"]["text"]) <= LOG_CAP


def test_carriage_returns_collapse(store):
    lg, _ = store.create_post("lead", "log", {}, live=True)
    store.append(lg["id"], "10%\r50%\r100%\nok\r\n")
    assert store.get_post(lg["id"])["payload"]["text"] == "100%\nok\n"


def test_normalize_table_shapes():
    want = {"columns": ["a", "b"], "rows": [["1", "2"]]}
    assert normalize_table("a,b\n1,2\n") == want
    assert normalize_table("a\tb\n1\t2\n") == want
    assert normalize_table('[["a","b"],["1","2"]]') == want
    assert normalize_table('[{"a": 1}, {"b": {"x": 1}}]') == {"columns": ["a", "b"], "rows": [[1, None], [None, '{"x": 1}']]}
    with pytest.raises(BoardError):
        normalize_table("[1, 2]")


def test_sniff_and_ansi():
    assert sniff_image(b"\x89PNG\r\n\x1a\n...") == "image/png"
    assert sniff_image(b"<svg xmlns='http://www.w3.org/2000/svg'/>") == "image/svg+xml"
    with pytest.raises(BoardError) as e:
        sniff_image(b"hello")
    assert e.value.status == 415
    assert strip_ansi("\x1b[1;31mred\x1b[0m \x1b]0;title\x07ok") == "red ok"


def test_list_posts_agent_prefix(store):
    for a in ("lead", "lead/tests", "leader", "other"):
        store.create_post(a, "text", {"text": a})
    posts, seq = store.list_posts("lead")
    assert sorted(p["agent"] for p in posts) == ["lead", "lead/tests"]
    assert seq == store.current_seq()


def _writer(path, n, w):
    s = Store(path)
    for i in range(n):
        s.create_post(f"w{w}", "text", {"text": str(i)})
    s.close()


def test_concurrent_writer_processes(board_dir):
    procs = [mp.get_context("spawn").Process(target=_writer, args=(str(board_dir), 25, w)) for w in range(6)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    s = Store(board_dir)
    posts, seq = s.list_posts(limit=1000)
    assert len(posts) == 150 and len({p["seq"] for p in posts}) == 150 and seq == 150
    s.close()


def test_reap_dead_logs_closes_orphaned_live_log(tmp_path):
    import socket
    import subprocess
    from board.store import Store
    s = Store(tmp_path)
    p = subprocess.Popen(["sleep", "0"]); p.wait()  # a pid that no longer exists
    dead, _ = s.create_post("lead", "log", {"pid": p.pid, "host": socket.gethostname()}, live=True)
    alive, _ = s.create_post("lead", "log", {"pid": os.getpid(), "host": socket.gethostname()}, live=True)
    other, _ = s.create_post("lead", "log", {"pid": p.pid, "host": "another-machine"}, live=True)
    assert s.reap_dead_logs() == 1
    d = s.get_post(dead["id"])
    assert d["live"] is False and d["status"] == "error" and "ended without reporting" in d["payload"]["text"]
    assert s.get_post(alive["id"])["live"] is True
    assert s.get_post(other["id"])["live"] is True
