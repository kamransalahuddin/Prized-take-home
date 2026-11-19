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
    assert sniff_image(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 17) == "image/png"
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


# ---------------------------------------------------------------- human replies


def test_reply_flips_blocked_to_info_and_emits_post_event(store):
    post, _ = store.create_post("lead", "text", {"text": "Can I?", "ask": True}, status="blocked")
    assert post["payload"] == {"text": "Can I?", "ask": True}
    seq0 = store.current_seq()
    out = store.reply(post["id"], "approve", "go ahead")
    assert out["status"] == "info" and out["payload"]["ask"] is True
    rep = out["payload"]["reply"]
    assert (rep["action"], rep["text"]) == ("approve", "go ahead") and rep["at"].endswith("Z")
    assert out["updated_at"] >= post["updated_at"] and out["seq"] > seq0
    (ev,) = store.events_after(seq0)
    assert ev["type"] == "post" and ev["data"]["payload"]["reply"] == rep and ev["data"]["status"] == "info"
    assert store.get_post(post["id"]) == out


def test_reply_errors(store):
    blocked, _ = store.create_post("lead", "text", {"text": "q"}, status="blocked")
    info, _ = store.create_post("lead", "text", {"text": "fyi"})
    with pytest.raises(BoardError) as e:
        store.reply("p_zzzzzz", "approve")
    assert e.value.status == 404
    with pytest.raises(BoardError) as e:
        store.reply(info["id"], "approve")
    assert (e.value.status, e.value.message) == (409, "post is not waiting for input")
    bad = (("maybe", None), ("answer", None), ("answer", "   "), ("answer", "x" * 10_001), ("approve", 5))
    for action, text in bad:
        with pytest.raises(BoardError) as e:
            store.reply(blocked["id"], action, text)
        assert e.value.status == 400, (action, text)
    assert store.reply(blocked["id"], "answer", "x" * 10_000)["payload"]["reply"]["action"] == "answer"
    with pytest.raises(BoardError) as e:  # double click
        store.reply(blocked["id"], "reject")
    assert (e.value.status, e.value.message) == (409, "already answered")


def test_reply_note_optional(store):
    p, _ = store.create_post("lead", "text", {"text": "q"}, status="blocked")
    assert store.reply(p["id"], "reject", "  ")["payload"]["reply"]["text"] is None


# ---------------------------------------------------------------- idempotent writes


def test_create_post_same_request_id_writes_once(store):
    a = store.create_post("lead", "text", {"text": "hi"}, request_id="r1")
    seq = store.current_seq()
    b = store.create_post("lead", "text", {"text": "hi"}, request_id="r1")
    assert a == b and store.current_seq() == seq
    assert len(store.list_posts()[0]) == 1


def test_append_same_request_id_stores_chunk_once(store):
    p, _ = store.create_post("lead", "log", {}, live=True)
    r1 = store.append(p["id"], text="line 1\n", request_id="c1")
    r2 = store.append(p["id"], text="line 1\n", request_id="c1")
    store.append(p["id"], text="line 2\n", request_id="c2")
    assert r1 == r2
    assert store.get_post(p["id"])["payload"]["text"] == "line 1\nline 2\n"


def test_reply_same_request_id_returns_first_result(store):
    p, _ = store.create_post("lead", "text", {"text": "q"}, status="blocked")
    a = store.reply(p["id"], "approve", request_id="x")
    assert store.reply(p["id"], "approve", request_id="x") == a  # retry, not a double click
    with pytest.raises(BoardError):
        store.reply(p["id"], "approve", request_id="y")
    assert sum(e["type"] == "post" for e in store.events_after(0)) == 2


def test_add_asset_same_request_id_once(store):
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 17
    a = store.add_asset(png, request_id="img")
    assert store.add_asset(png, request_id="img") == a
    assert store.conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1
    assert len(list(store.assets_dir.glob("a_*"))) == 1


def test_request_id_reused_for_other_op_and_pruning(store):
    p, _ = store.create_post("lead", "log", {}, request_id="same")
    with pytest.raises(BoardError) as e:
        store.append(p["id"], text="x", request_id="same")
    assert e.value.status == 409
    store.conn.execute("INSERT INTO requests(id, result, created_at) VALUES ('old', '{}', '2000-01-01T00:00:00.000Z')")
    store.append(p["id"], text="x", request_id="new")
    ids = {r[0] for r in store.conn.execute("SELECT id FROM requests")}
    assert ids == {"same", "new"}
