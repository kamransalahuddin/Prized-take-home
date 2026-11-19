"""Server tests: REST via TestClient; SSE + durability against a real uvicorn server."""

from __future__ import annotations

import json
import queue
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from board.server import create_app
from board.store import Store

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"></svg>'


def text_post(agent="lead", text="hi", **kw):
    return {"agent": agent, "kind": "text", "payload": {"text": text}, **kw}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        yield c


# ---------------------------------------------------------------- REST


def test_create_and_get_post(client):
    r = client.post("/api/posts", json=text_post(title="t"))
    assert r.status_code == 201
    p = r.json()
    assert p["created"] is True and p["agent"] == "lead" and p["payload"] == {"text": "hi"} and p["seq"] >= 1
    g = client.get(f"/api/posts/{p['id']}")
    assert g.status_code == 200 and g.json()["title"] == "t"
    lst = client.get("/api/posts").json()
    assert [x["id"] for x in lst["posts"]] == [p["id"]] and lst["seq"] == p["seq"]


def test_key_upsert_201_then_200(client):
    a = client.post("/api/posts", json=text_post(key="k", text="one"))
    b = client.post("/api/posts", json=text_post(key="k", text="two"))
    assert a.status_code == 201 and b.status_code == 200
    assert b.json()["created"] is False and b.json()["id"] == a.json()["id"]
    assert b.json()["payload"]["text"] == "two"
    assert len(client.get("/api/posts").json()["posts"]) == 1


def test_list_filter_and_limit(client):
    client.post("/api/posts", json=text_post(agent="lead"))
    client.post("/api/posts", json=text_post(agent="lead/tests"))
    client.post("/api/posts", json=text_post(agent="other"))
    assert len(client.get("/api/posts", params={"agent": "lead"}).json()["posts"]) == 2
    assert len(client.get("/api/posts", params={"limit": 1}).json()["posts"]) == 1
    assert client.get("/api/posts", params={"limit": "x"}).status_code == 400


def test_errors_are_json(client):
    r = client.post("/api/posts", json={"agent": "lead", "kind": "nope", "payload": {}})
    assert r.status_code == 415 and "error" in r.json()
    r = client.post("/api/posts", json={"kind": "text", "payload": {"text": "x"}})
    assert r.status_code == 400 and "agent" in r.json()["error"]
    r = client.post("/api/posts", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400 and "error" in r.json()
    r = client.get("/api/posts/p_nothere")
    assert r.status_code == 404 and r.json() == {"error": "no post p_nothere"}


def test_body_limit_413(client):
    big = "x" * (8 * 1024 * 1024 + 10)
    r = client.post("/api/posts", json=text_post(text=big))
    assert r.status_code == 413 and "error" in r.json()


def test_append_ok_404_409(client):
    log = client.post("/api/posts", json={"agent": "lead", "kind": "log", "payload": {}, "live": True}).json()
    r = client.post(f"/api/posts/{log['id']}/append", json={"text": "line 1\n"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"id", "seq", "status", "title", "live", "updated_at", "size"}
    assert body["size"] == 7 and body["live"] is True
    r = client.post(f"/api/posts/{log['id']}/append", json={"done": True, "exit_code": 0})
    assert r.json()["live"] is False and r.json()["status"] == "ok"
    assert client.get(f"/api/posts/{log['id']}").json()["payload"]["text"] == "line 1\n"

    assert client.post("/api/posts/p_nothere/append", json={"text": "x"}).status_code == 404
    table = client.post(
        "/api/posts", json={"agent": "lead", "kind": "table", "payload": {"columns": ["a"], "rows": [[1]]}}
    ).json()
    r = client.post(f"/api/posts/{table['id']}/append", json={"text": "x"})
    assert r.status_code == 409 and "error" in r.json()


def test_asset_upload_and_headers(client):
    r = client.post("/api/assets", files={"file": ("x.png", PNG, "application/octet-stream")})
    assert r.status_code == 201
    a = r.json()
    assert a["mime"] == "image/png" and a["size"] == len(PNG) and a["url"] == f"/api/assets/{a['id']}"
    g = client.get(a["url"])
    assert g.status_code == 200 and g.content == PNG
    assert g.headers["content-type"] == "image/png"
    assert g.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert g.headers["x-content-type-options"] == "nosniff"
    assert g.headers["content-security-policy"] == "default-src 'none'; style-src 'unsafe-inline'; sandbox"

    s = client.post("/api/assets", files={"file": ("x.svg", SVG, "image/svg+xml")}).json()
    gs = client.get(s["url"])
    assert gs.headers["content-type"].startswith("image/svg+xml") and "sandbox" in gs.headers["content-security-policy"]

    img = client.post("/api/posts", json={"agent": "lead", "kind": "image", "payload": {"asset_id": a["id"]}})
    assert img.status_code == 201 and img.json()["payload"]["url"] == a["url"]


def test_asset_rejects_non_image(client):
    r = client.post("/api/assets", files={"file": ("x.txt", b"hello world", "image/png")})
    assert r.status_code == 415 and "error" in r.json()
    assert client.post("/api/assets", data={"nofile": "1"}).status_code == 400
    assert client.get("/api/assets/a_missing").status_code == 404


def test_index_and_static(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    v = client.get("/static/vendor/purify.min.js")
    assert v.status_code == 200


# ---------------------------------------------------------------- real server helpers


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    def __init__(self, data_dir):
        self.app = create_app(data_dir)
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning",
                             timeout_graceful_shutdown=2)
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            assert time.time() < deadline, "server did not start"
            time.sleep(0.02)
        return self

    def stop(self):
        t0 = time.time()
        self.app.state.hub.request_stop()  # what the SIGINT hook does in `board serve`
        self.server.should_exit = True
        self.thread.join(10)
        assert not self.thread.is_alive()
        return time.time() - t0


@pytest.fixture
def live(tmp_path):
    srv = LiveServer(tmp_path).start()
    yield srv
    if srv.thread.is_alive():
        srv.stop()


class SSE:
    """Reads an SSE stream in a thread; parsed events land in `self.q`."""

    def __init__(self, url, headers=None, params=None):
        self.q: queue.Queue = queue.Queue()
        self.retry = None
        self.raw: list[str] = []
        self._client = httpx.Client(timeout=httpx.Timeout(5, read=30))
        self._ctx = self._client.stream("GET", url, headers=headers or {}, params=params)
        self.resp = self._ctx.__enter__()
        self.ended = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        ev = {}
        try:
            for line in self.resp.iter_lines():
                self.raw.append(line)
                if line == "":
                    if ev:
                        self.q.put(ev)
                    ev = {}
                elif line.startswith(":"):
                    self.q.put({"comment": line})
                else:
                    k, _, v = line.partition(":")
                    v = v[1:] if v.startswith(" ") else v
                    if k == "retry":
                        self.retry = int(v)
                    elif k == "data":
                        ev["data"] = json.loads(v)
                    elif k == "id":
                        ev["id"] = int(v)
                    else:
                        ev[k] = v
        except Exception:
            pass
        finally:
            self.ended.set()

    def next(self, timeout=2.0):
        return self.q.get(timeout=timeout)

    def take(self, n, timeout=2.0):
        return [self.next(timeout) for _ in range(n)]

    def assert_quiet(self, wait=0.4):
        with pytest.raises(queue.Empty):
            self.q.get(timeout=wait)

    def close(self):
        try:
            self._ctx.__exit__(None, None, None)
        finally:
            self._client.close()


# ---------------------------------------------------------------- SSE


def test_sse_headers_and_hello(live):
    Store(live.app.state.data_dir).create_post("lead", "text", {"text": "before"})
    s = SSE(live.url + "/api/events")
    try:
        assert s.resp.headers["content-type"].startswith("text/event-stream")
        assert s.resp.headers["cache-control"] == "no-cache"
        assert s.resp.headers["x-accel-buffering"] == "no"
        hello = s.next()
        assert hello["event"] == "hello" and hello["data"] == {"seq": 1}
        assert s.retry == 1000 and s.raw[0] == "retry: 1000"
        s.assert_quiet()  # no since → live only, nothing replayed
    finally:
        s.close()


def test_sse_cli_write_arrives_within_1s(live):
    s = SSE(live.url + "/api/events")
    try:
        assert s.next()["event"] == "hello"
        cli = Store(live.app.state.data_dir)  # a separate process's connection, like the CLI
        t0 = time.monotonic()
        post, _ = cli.create_post("lead/tests", "log", {}, title="pytest", live=True)
        ev = s.next(timeout=1.0)
        assert time.monotonic() - t0 < 1.0
        assert ev["event"] == "post" and ev["id"] == post["seq"] and ev["data"]["id"] == post["id"]
        assert ev["data"]["seq"] == ev["id"]

        res = cli.append(post["id"], text="chunk\n")
        ev = s.next(timeout=1.0)
        assert ev["event"] == "append" and ev["id"] == res["seq"]
        assert ev["data"]["text"] == "chunk\n" and ev["data"]["live"] is True and ev["data"]["kind"] == "log"
        cli.close()
    finally:
        s.close()


def test_sse_http_write_is_broadcast(live):
    s = SSE(live.url + "/api/events")
    try:
        s.next()
        r = httpx.post(live.url + "/api/posts", json=text_post(text="via http"))
        ev = s.next(timeout=1.0)
        assert ev["event"] == "post" and ev["data"]["id"] == r.json()["id"]
        s.assert_quiet()  # exactly once
    finally:
        s.close()


def test_sse_replay_last_event_id_exact_in_order(live):
    cli = Store(live.app.state.data_dir)
    s = SSE(live.url + "/api/events")
    s.next()
    first = [cli.create_post("lead", "text", {"text": f"a{i}"})[0]["seq"] for i in range(3)]
    got = s.take(3)
    assert [e["id"] for e in got] == first
    last_seen = got[-1]["id"]
    s.close()

    # while disconnected: a batch of writes from "other processes"
    missed = [cli.create_post("lead", "text", {"text": f"b{i}"})[0]["seq"] for i in range(5)]
    log, _ = cli.create_post("lead", "log", {})
    missed.append(log["seq"])
    missed.append(cli.append(log["id"], text="x\n")["seq"])

    s2 = SSE(live.url + "/api/events", headers={"Last-Event-ID": str(last_seen)})
    try:
        hello = s2.next()
        assert hello["event"] == "hello" and hello["data"]["seq"] == missed[-1]
        replay = s2.take(len(missed))
        assert [e["id"] for e in replay] == missed
        assert replay[-1]["event"] == "append"
        # live continues after replay, without duplicates
        new = cli.create_post("lead", "text", {"text": "after"})[0]["seq"]
        assert s2.next(1.0)["id"] == new
        s2.assert_quiet()
    finally:
        s2.close()
        cli.close()


def test_sse_replay_dedupes_when_writes_race_reconnect(live):
    """Events committed while the replay runs reach the client exactly once."""
    cli = Store(live.app.state.data_dir)
    for i in range(50):
        cli.create_post("lead", "text", {"text": f"pre{i}"})
    stop = threading.Event()
    written: list[int] = []

    def writer():
        w = Store(live.app.state.data_dir)
        i = 0
        while not stop.is_set() and i < 60:
            written.append(w.create_post("lead", "text", {"text": f"race{i}"})[0]["seq"])
            i += 1
            time.sleep(0.005)
        w.close()

    t = threading.Thread(target=writer)
    t.start()
    s = SSE(live.url + "/api/events", params={"since": 0})
    try:
        t.join()
        total = 50 + len(written)
        evs = [e for e in s.take(total + 1, timeout=3.0) if e.get("event") != "hello"][:total]
        ids = [e["id"] for e in evs]
        assert ids == list(range(1, total + 1))
        s.assert_quiet()
    finally:
        stop.set()
        s.close()
        cli.close()


def test_sse_since_query(live):
    cli = Store(live.app.state.data_dir)
    seqs = [cli.create_post("lead", "text", {"text": str(i)})[0]["seq"] for i in range(4)]
    s = SSE(live.url + "/api/events", params={"since": seqs[1]})
    try:
        assert s.next()["event"] == "hello"
        assert [e["id"] for e in s.take(2)] == seqs[2:]
        s.assert_quiet()
    finally:
        s.close()
        cli.close()


def test_sse_header_wins_over_since(live):
    cli = Store(live.app.state.data_dir)
    seqs = [cli.create_post("lead", "text", {"text": str(i)})[0]["seq"] for i in range(3)]
    s = SSE(live.url + "/api/events", headers={"Last-Event-ID": str(seqs[1])}, params={"since": 0})
    try:
        s.next()
        assert [e["id"] for e in s.take(1)] == seqs[2:]
        s.assert_quiet()
    finally:
        s.close()
        cli.close()


def test_sse_reset_when_since_is_from_another_db(live):
    s = SSE(live.url + "/api/events", params={"since": 999})
    try:
        assert s.next()["event"] == "hello"
        ev = s.next()
        assert ev["event"] == "reset" and ev["data"] == {"seq": 0}
    finally:
        s.close()


def test_sse_disconnect_unsubscribes(live):
    s = SSE(live.url + "/api/events")
    s.next()
    hub = live.app.state.hub
    assert len(hub.subs) == 1
    s.close()
    deadline = time.time() + 3
    while hub.subs and time.time() < deadline:
        time.sleep(0.05)
    assert not hub.subs


def test_shutdown_with_open_stream_is_prompt(tmp_path):
    srv = LiveServer(tmp_path).start()
    s = SSE(srv.url + "/api/events")
    s.next()
    took = srv.stop()
    assert took < 1.5, f"shutdown took {took:.2f}s with an open SSE stream"
    assert s.ended.wait(2)
    s.close()


def test_durability_across_restart(tmp_path):
    srv = LiveServer(tmp_path).start()
    ids = [httpx.post(srv.url + "/api/posts", json=text_post(text=f"p{i}")).json()["id"] for i in range(3)]
    srv.stop()

    srv2 = LiveServer(tmp_path).start()
    try:
        body = httpx.get(srv2.url + "/api/posts").json()
        assert sorted(p["id"] for p in body["posts"]) == sorted(ids)
        assert body["seq"] == 3
    finally:
        srv2.stop()


# ---------------------------------------------------------------- replies


def blocked_post(client):
    body = {"agent": "lead", "kind": "text", "payload": {"text": "Can I?", "ask": True}, "status": "blocked"}
    r = client.post("/api/posts", json=body)
    assert r.status_code == 201
    return r.json()


def test_reply_ok_and_double_click(client):
    p = blocked_post(client)
    assert p["payload"]["ask"] is True
    url = f"/api/posts/{p['id']}/reply"
    r = client.post(url, json={"action": "answer", "text": "keep the table"})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == p["id"] and body["status"] == "info" and body["seq"] > p["seq"]
    assert body["payload"]["reply"]["action"] == "answer" and body["payload"]["reply"]["text"] == "keep the table"
    assert client.get(f"/api/posts/{p['id']}").json() == body
    r2 = client.post(url, json={"action": "answer", "text": "keep the table"})  # second identical click
    assert (r2.status_code, r2.json()) == (409, {"error": "already answered"})


def test_reply_error_paths(client):
    p = blocked_post(client)
    info = client.post("/api/posts", json=text_post()).json()
    url = f"/api/posts/{p['id']}/reply"
    r = client.post("/api/posts/p_zzzzzz/reply", json={"action": "approve"})
    assert r.status_code == 404 and "error" in r.json()
    r = client.post(f"/api/posts/{info['id']}/reply", json={"action": "approve"})
    assert (r.status_code, r.json()) == (409, {"error": "post is not waiting for input"})
    bad = ({"action": "nope"}, {}, {"action": 1}, {"action": "answer"}, {"action": "answer", "text": ""},
           {"action": "answer", "text": "x" * 10_001}, {"action": "approve", "text": 3})
    for body in bad:
        r = client.post(url, json=body)
        assert r.status_code == 400 and "error" in r.json(), body
    r = client.post(url, json={"action": "approve"})
    assert r.status_code == 200
    assert r.json()["payload"]["reply"]["action"] == "approve" and r.json()["payload"]["reply"]["text"] is None


def test_reply_event_broadcast(live):
    s = SSE(live.url + "/api/events")
    try:
        s.next()
        p = httpx.post(live.url + "/api/posts", json=text_post(status="blocked")).json()
        assert s.next(timeout=1.0)["data"]["status"] == "blocked"
        httpx.post(live.url + f"/api/posts/{p['id']}/reply", json={"action": "reject", "text": "no"})
        ev = s.next(timeout=1.0)
        assert ev["event"] == "post" and ev["data"]["status"] == "info"
        assert ev["data"]["payload"]["reply"]["action"] == "reject"
        s.assert_quiet()
    finally:
        s.close()


# ---------------------------------------------------------------- idempotency (lost responses)


def test_same_request_posted_twice_writes_once(client):
    body = text_post(request_id="req-1")
    a, b = client.post("/api/posts", json=body), client.post("/api/posts", json=body)
    assert a.json() == b.json()
    assert len(client.get("/api/posts").json()["posts"]) == 1

    log = client.post("/api/posts", json={"agent": "lead", "kind": "log", "payload": {}, "live": True}).json()
    for _ in range(2):
        r = client.post(f"/api/posts/{log['id']}/append", json={"text": "a\n", "request_id": "c-1"})
        assert r.status_code == 200
    client.post(f"/api/posts/{log['id']}/append", json={"text": "b\n", "request_id": "c-2"})
    assert client.get(f"/api/posts/{log['id']}").json()["payload"]["text"] == "a\nb\n"

    q = client.post("/api/posts", json=text_post(status="blocked")).json()
    rb = {"action": "approve", "request_id": "rep-1"}
    r1 = client.post(f"/api/posts/{q['id']}/reply", json=rb)
    r2 = client.post(f"/api/posts/{q['id']}/reply", json=rb)
    assert r1.status_code == r2.status_code == 200 and r1.json() == r2.json()


def test_asset_upload_retry_writes_once(client, tmp_path):
    def up(**kw):
        return client.post("/api/assets", files={"file": ("x", PNG)}, **kw).json()

    a, b = up(headers={"X-Request-Id": "up-1"}), up(headers={"X-Request-Id": "up-1"})
    c, d = up(data={"request_id": "up-2"}), up(data={"request_id": "up-2"})
    assert a == b and c == d and a["id"] != c["id"]
    assert len(list((tmp_path / "assets").glob("a_*"))) == 2
