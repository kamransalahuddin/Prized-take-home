"""board ask --text: an open / either-or question answered only with typed text. Found by
fresh agents: Approve/Reject on "drop or merge?" left the agent guessing."""

import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from board.server import create_app
from board.store import BoardError, Store


def test_store_text_question_takes_only_an_answer(tmp_path):
    s = Store(tmp_path)
    p, _ = s.create_post(agent="data", kind="text", payload={"text": "Drop or merge?", "ask": "text"}, status="blocked")
    assert s.get_post(p["id"])["payload"]["ask"] == "text"
    for action in ("approve", "reject"):
        with pytest.raises(BoardError, match="typed answer"):
            s.reply(p["id"], action)
    r = s.reply(p["id"], "answer", "merge them")
    assert (r["status"], r["payload"]["reply"]["action"], r["payload"]["reply"]["text"]) == ("info", "answer", "merge them")


def test_store_rejects_other_ask_values(tmp_path):
    with pytest.raises(BoardError):
        Store(tmp_path).create_post(agent="a", kind="text", payload={"text": "x", "ask": "yes"})


def test_http_reply_to_text_question(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        p = c.post("/api/posts", json={"agent": "data", "kind": "text", "status": "blocked",
                                       "payload": {"text": "Drop or merge?", "ask": "text"}}).json()
        url = f"/api/posts/{p['id']}/reply"
        assert c.post(url, json={"action": "approve"}).status_code == 400
        r = c.post(url, json={"action": "answer", "text": "drop"})
        assert r.status_code == 200 and r.json()["payload"]["reply"]["text"] == "drop"


def test_cli_ask_text_end_to_end(board_env, store):
    proc = subprocess.Popen([sys.executable, "-m", "board.cli", "ask", "-a", "data", "--text", "Drop or merge?"],
                            env=board_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            stdin=subprocess.DEVNULL)
    pid = None
    for _ in range(100):
        posts, _ = store.list_posts()
        if posts:
            pid = posts[0]["id"]
            break
        time.sleep(0.05)
    assert pid and store.get_post(pid)["payload"]["ask"] == "text"
    store.reply(pid, "answer", "merge them")
    out, err = proc.communicate(timeout=20)
    assert proc.returncode == 0, err
    assert out.splitlines() == ["answer", "merge them"]
