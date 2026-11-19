"""SSE / event-log regressions from the network-interruptions QA pass (batch 5)."""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from board import server as server_mod
from board import store as store_mod
from board.server import Hub, Subscriber, create_app
from board.store import Store


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Live:
    def __init__(self, data_dir):
        self.app = create_app(data_dir)
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port,
                                                    log_level="warning", timeout_graceful_shutdown=2))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        while not self.server.started:
            time.sleep(0.02)

    def stop(self):
        self.app.state.hub.request_stop()
        self.server.should_exit = True
        self.thread.join(10)


def read_events(url, headers=None, params=None, until=None, n=50, timeout=5.0):
    """Read SSE events (dicts with event/id/data) until `until(ev)` or n events."""
    out, ev = [], {}
    with httpx.stream("GET", url, headers=headers or {}, params=params,
                      timeout=httpx.Timeout(5, read=timeout)) as r:
        for line in r.iter_lines():
            if line == "":
                if "event" in ev:
                    out.append(ev)
                    if (until and until(ev)) or len(out) >= n:
                        return out
                ev = {}
            elif not line.startswith(":"):
                k, _, v = line.partition(": ")
                ev[k] = json.loads(v) if k == "data" else v
    return out


# ---------------------------------------------------------------- 1. small events, byte-bounded batches


def test_status_change_on_big_log_is_a_small_append_event(store):
    p, _ = store.create_post("x", "log", {}, live=True)
    store.append(p["id"], text="y" * 1_000_000)
    for i in range(5):
        store.append(p["id"], title=f"t{i}", status="warn")
    store.append(p["id"], done=True, exit_code=1)
    evs = store.events_after(p["seq"] + 1)
    assert [e["type"] for e in evs] == ["append"] * 6
    assert all(e["data"]["text"] == "" for e in evs)
    assert evs[-1]["data"]["live"] is False and evs[-1]["data"]["exit_code"] == 1
    assert evs[-1]["data"]["status"] == "error"
    total = store.conn.execute("SELECT SUM(LENGTH(data)) FROM events").fetchone()[0]
    assert total < 1_100_000  # the log text is in the event table once, not once per change


def test_events_after_is_byte_bounded(store):
    p, _ = store.create_post("x", "log", {}, live=True)
    for _ in range(10):
        store.append(p["id"], text="z" * 300_000)
    batch = store.events_after(0, 1000, 1024 * 1024)
    assert 1 <= len(batch) <= 5
    big, _ = store.create_post("x", "markdown", {"markdown": "m" * 3_000_000})
    assert len(store.events_after(big["seq"] - 1, 1000, 1024)) == 1  # always at least one event
    assert store.events_after(store.current_seq()) == []


# ---------------------------------------------------------------- 2. per-subscriber byte budget


def test_slow_subscriber_dropped_on_byte_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "QUEUE_MAX_BYTES", 10_000)

    async def go():
        hub = Hub(Store(tmp_path))
        hub.stopping = asyncio.Event()
        slow, ok = hub.subscribe(), hub.subscribe()
        for i in range(30):
            hub.publish(i, b"x" * 1000)
            ok.get_nowait_frame(ok.queue.get_nowait())  # `ok` keeps up
        assert slow.closed and slow not in hub.subs and ok in hub.subs
        assert slow.queue.qsize() == 1 and slow.queue.get_nowait() is None  # frames freed at once
        assert slow.bytes == 0

    asyncio.run(go())


# ---------------------------------------------------------------- 3. stalled / half-open clients


def test_stalled_client_is_cut_off(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "SEND_TIMEOUT", 1.0)
    monkeypatch.setattr(server_mod, "QUEUE_MAX_BYTES", 10**9)  # exercise the send timeout, not the budget
    live = Live(tmp_path)
    try:
        socks = []
        for _ in range(3):
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            s.connect(("127.0.0.1", live.port))
            s.sendall(b"GET /api/events HTTP/1.1\r\nHost: x\r\n\r\n")  # ...and never read
            socks.append(s)
        hub = live.app.state.hub
        deadline = time.time() + 5
        while len(hub.subs) < 3 and time.time() < deadline:
            time.sleep(0.05)
        assert len(hub.subs) == 3
        st = Store(tmp_path)
        for _ in range(12):
            st.create_post("x", "markdown", {"markdown": "m" * 1_000_000})
        deadline = time.time() + 20
        while hub.subs and time.time() < deadline:
            time.sleep(0.1)
        assert not hub.subs, "stalled clients were never dropped"
        # ...and their connections were aborted (not left waiting to flush): reading now ends
        for s in socks:
            s.settimeout(5)
            try:
                while s.recv(1 << 20):
                    pass
            except ConnectionResetError:
                pass  # aborted: RST
        # a healthy client still works
        evs = read_events(live.url + "/api/events", until=lambda e: e.get("event") == "hello")
        assert evs[-1]["event"] == "hello"
        for s in socks:
            s.close()
        st.close()
    finally:
        live.stop()


def test_pings_are_sent_while_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "PING_INTERVAL", 0.3)
    live = Live(tmp_path)
    stop = threading.Event()

    def writer():
        st = Store(tmp_path)
        p, _ = st.create_post("x", "log", {}, live=True)
        while not stop.is_set():
            st.append(p["id"], text="busy\n")
            time.sleep(0.02)
        st.close()

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        pings = 0
        t0 = time.time()
        with httpx.stream("GET", live.url + "/api/events", timeout=5) as r:
            for line in r.iter_lines():
                pings += line == "event: ping"
                if line.startswith("data: {\"t\""):
                    assert isinstance(json.loads(line[6:])["t"], int)
                if time.time() - t0 > 1.5:
                    break
        assert pings >= 2
    finally:
        stop.set()
        t.join(5)
        live.stop()


# ---------------------------------------------------------------- 4. database epoch


def test_epoch_in_snapshot_hello_and_ids_and_reset_on_other_db(tmp_path):
    d = tmp_path / "data"
    live = Live(d)
    try:
        snap = httpx.get(live.url + "/api/posts").json()
        epoch = snap["epoch"]
        httpx.post(live.url + "/api/posts", json={"agent": "x", "kind": "text", "payload": {"text": "a"}})
        evs = read_events(live.url + "/api/events", params={"since": snap["seq"]},
                          until=lambda e: e.get("event") == "post")
        assert evs[0]["event"] == "hello" and evs[0]["data"]["epoch"] == epoch
        assert evs[-1]["id"] == f"{epoch}:{evs[-1]['data']['seq']}"
        last = evs[-1]["id"]
        # resume with EPOCH:SEQ from this DB: no reset
        evs = read_events(live.url + "/api/events", headers={"Last-Event-ID": last},
                          until=lambda e: e.get("event") in ("hello",), n=1)
        assert evs[0]["event"] == "hello"
    finally:
        live.stop()
    shutil.rmtree(d)  # wiped; a new DB gets a new epoch and its seq restarts
    live = Live(d)
    try:
        for i in range(3):
            httpx.post(live.url + "/api/posts", json={"agent": "x", "kind": "text", "payload": {"text": f"n{i}"}})
        evs = read_events(live.url + "/api/events", headers={"Last-Event-ID": last}, n=2)
        assert [e["event"] for e in evs] == ["hello", "reset"]  # old epoch: refetch, not stale events
        assert evs[1]["data"]["epoch"] != epoch
    finally:
        live.stop()


# ---------------------------------------------------------------- 5. exact log truncation


def test_log_truncation_keeps_exactly_last_cap_code_points(store, monkeypatch):
    monkeypatch.setattr(store_mod, "LOG_CAP", 10)
    p, _ = store.create_post("x", "log", {}, live=True)
    sent = ""
    for chunk in ("abc", "défgh", "ïjklmnop", "q", "€" * 25, "rs"):
        store.append(p["id"], text=chunk)
        sent += chunk
        assert store.get_post(p["id"])["payload"]["text"] == sent[-10:]
    assert store.get_post(p["id"])["payload"]["truncated"] is True
