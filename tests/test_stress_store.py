"""Store-level regressions from the stress QA passes (duplicate writes, replaced posts)."""

import sqlite3

import pytest

from board.store import BoardError, Store


def test_request_id_reused_with_different_body_is_409(store):
    a, _ = store.create_post("alice", "text", {"text": "mine"}, request_id="dup")
    with pytest.raises(BoardError) as e:
        store.create_post("bob", "text", {"text": "secret"}, request_id="dup")
    assert e.value.status == 409 and "different request" in e.value.message
    assert store.create_post("alice", "text", {"text": "mine"}, request_id="dup")[0] == a  # true retry
    p, _ = store.create_post("lead", "log", {}, live=True)
    q, _ = store.create_post("lead", "log", {}, live=True)
    store.append(p["id"], text="one\n", request_id="c")
    for kw in ({"post_id": q["id"], "text": "one\n"}, {"post_id": p["id"], "text": "two\n"}):
        with pytest.raises(BoardError) as e:
            store.append(request_id="c", **kw)
        assert e.value.status == 409
    assert store.get_post(q["id"])["payload"]["text"] == ""
    b1, _ = store.create_post("lead", "text", {"text": "q1"}, status="blocked")
    b2, _ = store.create_post("lead", "text", {"text": "q2"}, status="blocked")
    store.reply(b1["id"], "approve", request_id="r")
    with pytest.raises(BoardError) as e:
        store.reply(b2["id"], "reject", "no", request_id="r")
    assert e.value.status == 409 and store.get_post(b2["id"])["status"] == "blocked"
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 17
    store.add_asset(png, request_id="img")
    with pytest.raises(BoardError):
        store.add_asset(png + b"x", request_id="img")


def test_old_database_is_migrated(board_dir):
    db = sqlite3.connect(board_dir / "board.db")
    db.executescript("""
    CREATE TABLE posts (id TEXT PRIMARY KEY, agent TEXT NOT NULL, kind TEXT NOT NULL, title TEXT,
      status TEXT NOT NULL DEFAULT 'info', key TEXT, payload TEXT NOT NULL, live INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL, seq INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE requests (id TEXT PRIMARY KEY, result TEXT NOT NULL, created_at TEXT NOT NULL);
    INSERT INTO posts VALUES ('p_aaaaaa','lead','text',NULL,'info',NULL,'{"text":"old"}',0,'x','x',0);
    INSERT INTO requests VALUES ('legacy','{"op":"append","result":{"id":"p_aaaaaa"}}',
                                 '2999-01-01T00:00:00.000Z');
    """)
    db.commit()
    db.close()
    s = Store(board_dir)
    assert len(s.get_post("p_aaaaaa")["gen"]) == 16
    assert s.append("p_aaaaaa", text="anything", request_id="legacy") == {"id": "p_aaaaaa"}  # legacy: op only
    s.create_post("lead", "text", {"text": "new"}, request_id="fresh")
    assert s.conn.execute("SELECT fp FROM requests WHERE id='fresh'").fetchone()[0]
    s.close()


def test_gen_changes_on_create_and_key_replace_not_on_append(store):
    p, _ = store.create_post("lead", "log", {}, key="job", live=True)
    store.append(p["id"], text="x", status="warn")
    assert store.get_post(p["id"])["gen"] == p["gen"]
    p2, created = store.create_post("lead", "log", {}, key="job", live=True)
    assert not created and p2["id"] == p["id"] and p2["gen"] != p["gen"]
    with pytest.raises(BoardError) as e:
        store.append(p["id"], text="old run\n", expect_gen=p["gen"])
    assert e.value.status == 409 and "replaced" in e.value.message
    with pytest.raises(BoardError):
        store.append(p["id"], done=True, exit_code=0, expect_gen=p["gen"])
    store.append(p["id"], text="new run\n", expect_gen=p2["gen"])
    post = store.get_post(p["id"])
    assert post["payload"]["text"] == "new run\n" and post["live"] is True


def test_reply_expect_gen_rejects_answer_to_replaced_question(store):
    q1, _ = store.create_post("lead", "text", {"text": "Restart staging?", "ask": True}, status="blocked", key="q")
    q2, _ = store.create_post("lead", "text", {"text": "DROP prod DB?", "ask": True}, status="blocked", key="q")
    with pytest.raises(BoardError) as e:
        store.reply(q1["id"], "approve", expect_gen=q1["gen"])
    assert e.value.status == 409
    assert store.get_post(q1["id"])["status"] == "blocked"
    assert store.reply(q1["id"], "reject", expect_gen=q2["gen"])["payload"]["reply"]["action"] == "reject"


def test_reply_replay_with_other_expect_gen_is_409(store):
    q1, _ = store.create_post("lead", "text", {"text": "v1"}, status="blocked", key="q")
    store.reply(q1["id"], "approve", request_id="R", expect_gen=q1["gen"])
    q2, _ = store.create_post("lead", "text", {"text": "v2"}, status="blocked", key="q")
    with pytest.raises(BoardError) as e:
        store.reply(q1["id"], "approve", request_id="R", expect_gen=q2["gen"])
    assert e.value.status == 409
    assert store.get_post(q1["id"])["status"] == "blocked"
