"""HTTP-level regressions from the stress QA passes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from board.server import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        yield c


def ask(client, text, **kw):
    r = client.post("/api/posts", json={"agent": "lead", "kind": "text", "status": "blocked",
                                        "payload": {"text": text, "ask": True}, **kw})
    assert r.status_code in (200, 201)
    return r.json()


def test_http_request_id_reused_with_different_body_is_409(client):
    a = client.post("/api/posts", json={"agent": "alice", "kind": "text", "payload": {"text": "a"},
                                        "request_id": "R"})
    assert a.status_code == 201
    b = client.post("/api/posts", json={"agent": "bob", "kind": "text", "payload": {"text": "b"},
                                        "request_id": "R"})
    assert b.status_code == 409 and "different request" in b.json()["error"]
    assert "a" == client.get(f"/api/posts/{a.json()['id']}").json()["payload"]["text"]
    assert len(client.get("/api/posts").json()["posts"]) == 1
    q2, q3 = ask(client, "q2")["id"], ask(client, "q3")["id"]
    assert client.post(f"/api/posts/{q2}/reply", json={"action": "approve", "request_id": "RR"}).status_code == 200
    r = client.post(f"/api/posts/{q3}/reply", json={"action": "reject", "text": "no", "request_id": "RR"})
    assert r.status_code == 409
    assert client.get(f"/api/posts/{q3}").json()["status"] == "blocked"


def test_http_post_has_gen_and_append_expect_gen(client):
    p = client.post("/api/posts", json={"agent": "lead", "kind": "log", "payload": {}, "key": "j",
                                        "live": True}).json()
    assert isinstance(p["gen"], str)
    p2 = client.post("/api/posts", json={"agent": "lead", "kind": "log", "payload": {}, "key": "j",
                                         "live": True}).json()
    assert p2["id"] == p["id"] and p2["gen"] != p["gen"]
    r = client.post(f"/api/posts/{p['id']}/append", json={"text": "x", "expect_gen": p["gen"]})
    assert r.status_code == 409 and "replaced" in r.json()["error"]
    assert client.post(f"/api/posts/{p['id']}/append", json={"text": "x", "expect_gen": p2["gen"]}).status_code == 200


def test_http_reply_expect_gen(client):
    q1 = ask(client, "Restart staging?", key="k")
    q2 = ask(client, "DROP prod DB?", key="k")
    r = client.post(f"/api/posts/{q1['id']}/reply", json={"action": "approve", "expect_gen": q1["gen"]})
    assert r.status_code == 409 and "replaced" in r.json()["error"]
    assert client.get(f"/api/posts/{q2['id']}").json()["status"] == "blocked"
    r = client.post(f"/api/posts/{q2['id']}/reply", json={"action": "reject", "expect_gen": q2["gen"]})
    assert r.status_code == 200 and r.json()["payload"]["reply"]["action"] == "reject"


def test_http_reply_replay_after_reask_with_other_gen_is_409(client):
    q1 = ask(client, "v1", key="K")
    body = {"action": "approve", "request_id": "RK1", "expect_gen": q1["gen"]}
    assert client.post(f"/api/posts/{q1['id']}/reply", json=body).status_code == 200
    q2 = ask(client, "v2", key="K")
    assert client.post(f"/api/posts/{q1['id']}/reply", json=body).status_code == 200  # true replay: stored
    r = client.post(f"/api/posts/{q1['id']}/reply", json=dict(body, expect_gen=q2["gen"]))
    assert r.status_code == 409
    assert client.get(f"/api/posts/{q1['id']}").json()["status"] == "blocked"
