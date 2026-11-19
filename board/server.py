"""HTTP API + live SSE push for the board (docs/contract.md sections 4-5).

One background watcher polls `PRAGMA data_version` every 100 ms (server-side only).
When anything commits - the CLI in another process or this server's own HTTP writes -
it reads `events_after(last_seq)` and fans the events out to one bounded asyncio.Queue
per SSE client. Broadcasting has a single path, and events are only read after commit,
so persist-before-broadcast holds.

Three Store connections: watcher, HTTP reads (incl. SSE replay) and HTTP writes. The
writer must be a different connection from the watcher, because `data_version` only
changes for commits made by *other* connections. Blocking DB work runs in the
threadpool; each shared connection is guarded by a lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import signal
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .store import MAX_ASSET, BoardError, Store, check_text, loads_strict

log = logging.getLogger("board.server")

STATIC_DIR = Path(__file__).parent / "static"
MAX_POST_BODY = 8 * 1024 * 1024
POLL_INTERVAL = 0.1
PING_INTERVAL = 15.0
QUEUE_MAX = 5000  # events buffered per SSE client before it is dropped (it reconnects + replays)
QUEUE_MAX_BYTES = 8 * 1024 * 1024  # ... or this many bytes of frames
REPLAY_BATCH = 500
BATCH_BYTES = 1024 * 1024  # replay / watcher read at most ~this much event data at a time
SEND_TIMEOUT = 30.0  # a client that doesn't take a frame for this long is cut off
RESUME_RE = re.compile(r"^(?:([0-9a-f]{1,32}):)?([0-9]{1,18})$", re.ASCII)

ASSET_HEADERS = {
    "Cache-Control": "public, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
}
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}

PLACEHOLDER = """<!doctype html><meta charset="utf-8"><title>board</title>
<p>board server is running. The page (board/static/index.html) is not built yet.</p>"""


class Locked:
    """A Store plus a lock; `call` runs a Store method in the threadpool under the lock."""

    def __init__(self, store: Store):
        self.store = store
        self.lock = threading.Lock()

    def _run(self, fn: Callable, *args, **kwargs):
        with self.lock:
            return fn(*args, **kwargs)

    async def call(self, name: str, *args, **kwargs):
        return await run_in_threadpool(self._run, getattr(self.store, name), *args, **kwargs)

    def _write(self, fn: Callable, *args, **kwargs):
        with self.lock:
            self.store.check_same_file()  # never report success for a write into a deleted DB
            return fn(*args, **kwargs)

    async def write(self, name: str, *args, **kwargs):
        return await run_in_threadpool(self._write, getattr(self.store, name), *args, **kwargs)

    def close(self) -> None:
        with self.lock:
            self.store.close()


class StalledClient(OSError):
    """An SSE client did not accept a frame within SEND_TIMEOUT."""


def _find_transport(send: Any) -> Any:
    """The asyncio transport behind an ASGI `send` (uvicorn's cycle object holds it, under a
    few layers of middleware closures), or None. Used to abort - not gracefully close - a
    connection whose peer stopped reading: close() would wait forever to flush."""
    seen, todo = set(), [send]
    while todo and len(seen) < 200:
        obj = todo.pop()
        if id(obj) in seen or obj is None:
            continue
        seen.add(id(obj))
        t = getattr(obj, "transport", None)
        if t is not None and hasattr(t, "abort"):
            return t
        todo.append(getattr(obj, "__self__", None))
        todo.append(getattr(obj, "func", None))  # functools.partial
        for cell in getattr(obj, "__closure__", None) or ():
            with contextlib.suppress(ValueError):
                if callable(cell.cell_contents) or hasattr(cell.cell_contents, "transport"):
                    todo.append(cell.cell_contents)
    return None


class SSEResponse(StreamingResponse):
    """StreamingResponse with a timeout on every send: a client that stops reading (or a
    half-open connection) is cut off instead of holding the generator and its memory.
    A send blocked on such a client also ends when the server shuts down (`stop`)."""

    def __init__(self, content, stop: asyncio.Event, **kw):
        super().__init__(content, **kw)
        self.stop = stop

    async def stream_response(self, send) -> None:
        stopper = asyncio.ensure_future(self.stop.wait())

        async def timed(message):
            task = asyncio.ensure_future(send(message))
            done, _ = await asyncio.wait({task, stopper}, timeout=SEND_TIMEOUT,
                                         return_when=asyncio.FIRST_COMPLETED)
            if task not in done:
                task.cancel()
                transport = _find_transport(send)
                if transport is not None:
                    transport.abort()  # release the socket now; the peer isn't reading
                raise StalledClient("SSE client stopped reading" if not self.stop.is_set()
                                    else "server shutting down")
            task.result()

        try:
            await timed({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
            async for chunk in self.body_iterator:
                await timed({"type": "http.response.body", "body": chunk, "more_body": True})
            await timed({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            stopper.cancel()
            with contextlib.suppress(Exception):
                await self.body_iterator.aclose()


class _QuietDisconnects(logging.Filter):
    """Don't log a traceback for every SSE client we cut off (stalled / disconnected)."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        while exc is not None:
            if isinstance(exc, StalledClient) or type(exc).__name__ == "ClientDisconnect":
                return False
            exc = exc.__cause__ or exc.__context__
        return True


logging.getLogger("uvicorn.error").addFilter(_QuietDisconnects())


def sse_frame(event: str, data: Any, eid: int | str | None = None) -> bytes:
    head = f"id: {eid}\n" if eid is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


class Subscriber:
    def __init__(self) -> None:
        # Unbounded so a close sentinel always fits; the bounds (frames and bytes) are
        # enforced in Hub.publish.
        self.queue: asyncio.Queue[tuple[int, bytes] | None] = asyncio.Queue()
        self.bytes = 0
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(None)

    def drop(self) -> None:
        """Too slow: free the buffered frames now and end the stream (it reconnects and
        replays from its Last-Event-ID)."""
        while not self.queue.empty():
            self.queue.get_nowait()
        self.bytes = 0
        self.closed = False
        self.close()

    def get_nowait_frame(self, item):
        if item is not None:
            self.bytes -= len(item[1])
        return item


class Hub:
    """Watches the database and fans committed events out to SSE subscribers."""

    def __init__(self, watch_store: Store):
        self.watch = watch_store  # used only by the watcher task, one call at a time
        self.subs: set[Subscriber] = set()
        self.last_seq = 0
        self.stopping = asyncio.Event()
        self.nudge = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        self.stopping = asyncio.Event()
        self.nudge = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self.last_seq = await run_in_threadpool(self.watch.current_seq)
        self.epoch = self.watch.epoch
        self._task = asyncio.create_task(self._run(), name="board-watcher")

    def stop(self) -> None:
        """Wake every SSE generator so it returns; safe to call more than once."""
        self.stopping.set()
        for sub in list(self.subs):
            sub.close()
        self.subs.clear()

    def request_stop(self) -> None:
        """Thread/signal-safe `stop`."""
        if self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.stop)

    async def aclose(self) -> None:
        self.stop()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    def subscribe(self) -> Subscriber:
        sub = Subscriber()
        if self.stopping.is_set():
            sub.close()
        else:
            self.subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self.subs.discard(sub)

    def publish(self, eid: int, frame: bytes) -> None:
        for sub in list(self.subs):
            if sub.closed:
                self.subs.discard(sub)
            elif sub.queue.qsize() >= QUEUE_MAX or sub.bytes + len(frame) > QUEUE_MAX_BYTES:
                log.warning("dropping slow SSE client (queue full)")
                self.subs.discard(sub)
                sub.drop()
            else:
                sub.bytes += len(frame)
                sub.queue.put_nowait((eid, frame))

    def frame(self, e: dict) -> bytes:
        return sse_frame(e["type"], e["data"], f"{self.epoch}:{e['id']}")

    async def _run(self) -> None:
        version = None
        checked, replaced = 0.0, False
        loop = asyncio.get_running_loop()
        while not self.stopping.is_set():
            if loop.time() - checked >= 1.0:
                checked = loop.time()
                try:
                    await run_in_threadpool(self.watch.check_same_file)
                    replaced = False
                except BoardError as e:
                    if not replaced:
                        print(f"board: ERROR {e.message}", file=sys.stderr, flush=True)
                    replaced = True
            try:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.nudge.wait(), POLL_INTERVAL)
                self.nudge.clear()
                v = await run_in_threadpool(self.watch.data_version)
                if v == version:
                    continue
                version = v  # read before the events: a commit in between bumps it again
                while True:  # in byte-bounded batches, publishing as we go
                    batch = await run_in_threadpool(self.watch.events_after, self.last_seq, 1000, BATCH_BYTES)
                    if not batch:
                        break
                    for e in batch:
                        if e["id"] <= self.last_seq:
                            continue
                        self.last_seq = e["id"]
                        self.publish(e["id"], self.frame(e))
            except asyncio.CancelledError:
                raise
            except Exception:  # keep watching; the next poll retries
                log.exception("board watcher error")
                version = None
                await asyncio.sleep(0.5)


def _err(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _install_shutdown_hook(hub: Hub) -> Callable[[], None]:
    """Uvicorn waits for open responses before shutting down; SSE streams never end on
    their own. Chain onto uvicorn's SIGINT/SIGTERM handlers so the streams close at once."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous: dict[int, Any] = {}

    def handler(sig, frame):
        hub.request_stop()
        prev = previous.get(sig)
        if callable(prev):
            prev(sig, frame)
        elif prev == signal.SIG_DFL and sig == signal.SIGINT:
            raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, handler)
        except (ValueError, OSError):  # pragma: no cover
            previous.pop(sig, None)

    def restore() -> None:
        for sig, prev in previous.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                if signal.getsignal(sig) is handler:
                    signal.signal(sig, prev)

    return restore


def _sweep_stale_tmp(assets_dir: Path, older_than: float = 600) -> None:
    """Remove half-written uploads (`.<id>.<ext>.tmp`) left by a crash."""
    import time as _time

    for f in assets_dir.glob(".*.tmp"):
        with contextlib.suppress(OSError):
            if _time.time() - f.stat().st_mtime > older_than:
                f.unlink()


def create_app(data_dir: Path | str | None = None) -> FastAPI:
    reader = Locked(Store(data_dir))
    _sweep_stale_tmp(reader.store.assets_dir)
    writer = Locked(Store(reader.store.dir))
    hub = Hub(Store(reader.store.dir))

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await hub.start()
        restore = _install_shutdown_hook(hub)

        async def reaper():  # close logs whose `board log` process was killed (-9)
            while True:
                with contextlib.suppress(Exception):
                    if await writer.write("reap_dead_logs"):
                        hub.nudge.set()
                await asyncio.sleep(5)

        with contextlib.suppress(Exception):
            await writer.write("sweep_orphan_assets")
        reap_task = asyncio.create_task(reaper())
        try:
            yield
        finally:
            reap_task.cancel()
            restore()
            await hub.aclose()
            for locked in (writer, reader):  # checkpoint the WAL on a clean shutdown
                with contextlib.suppress(Exception):
                    locked.close()
            with contextlib.suppress(Exception):
                hub.watch.close()

    app = FastAPI(title="board", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.hub = hub
    app.state.reader = reader
    app.state.writer = writer
    app.state.data_dir = reader.store.dir

    @app.exception_handler(BoardError)
    async def _board_error(request: Request, exc: BoardError):
        return _err(exc.message, exc.status)

    @app.exception_handler(sqlite3.Error)
    async def _db_error(request: Request, exc: sqlite3.Error):
        log.error("database error: %s", exc)
        return _err(f"board database error: {exc}", 503)

    @app.exception_handler(OSError)
    async def _os_error(request: Request, exc: OSError):
        log.error("storage error: %s", exc)
        return _err(f"board storage error: {exc.strerror or exc}", 503)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        return _err(str(exc.detail), exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        errs = exc.errors()
        msg = "; ".join(f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg')}" for e in errs) or "bad request"
        return _err(msg, 400)

    async def read_json(request: Request) -> dict:
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_POST_BODY:
            raise BoardError("request body is larger than 8 MB", 413)
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_POST_BODY:
                raise BoardError("request body is larger than 8 MB", 413)
        try:
            text = bytes(body or b"null").decode("utf-8")
        except UnicodeDecodeError as e:
            raise BoardError(f"invalid JSON body: not UTF-8 ({e})")
        data = loads_strict(text, "invalid JSON body")
        if not isinstance(data, dict):
            raise BoardError("request body must be a JSON object")
        return data

    def opt_bool(d: dict, name: str) -> bool:
        v = d.get(name, False)
        if v is None:
            return False
        if not isinstance(v, bool):
            raise BoardError(f"{name} must be true or false")
        return v

    def opt_str(d: dict, name: str) -> str | None:
        v = d.get(name)
        if v is not None and not isinstance(v, str):
            raise BoardError(f"{name} must be a string")
        return check_text(v, name)

    # ------------------------------------------------------------------ REST

    @app.post("/api/posts")
    async def create_post(request: Request):
        d = await read_json(request)
        if not d.get("kind"):
            raise BoardError("kind is required: text, markdown, table, chart, image or log")
        if not isinstance(d["kind"], str):
            raise BoardError("kind must be a string")
        agent = opt_str(d, "agent")
        post, created = await writer.write(
            "create_post",
            agent,
            d["kind"],
            d.get("payload"),
            title=opt_str(d, "title"),
            status=opt_str(d, "status"),
            key=opt_str(d, "key"),
            live=opt_bool(d, "live"),
            request_id=opt_str(d, "request_id"),
        )
        hub.nudge.set()
        return JSONResponse({**post, "created": created}, status_code=201 if created else 200)

    @app.post("/api/posts/{post_id}/append")
    async def append(post_id: str, request: Request):
        d = await read_json(request)
        exit_code = d.get("exit_code")
        if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
            raise BoardError("exit_code must be an integer")
        res = await writer.write(
            "append",
            post_id,
            text=opt_str(d, "text"),
            status=opt_str(d, "status"),
            title=opt_str(d, "title"),
            done=opt_bool(d, "done"),
            exit_code=exit_code,
            newline=opt_bool(d, "newline"),
            expect_kind=opt_str(d, "expect_kind"),
            expect_gen=opt_str(d, "expect_gen"),
            request_id=opt_str(d, "request_id"),
        )
        hub.nudge.set()
        return res

    @app.post("/api/posts/{post_id}/reply")
    async def reply(post_id: str, request: Request):
        d = await read_json(request)
        action = d.get("action")
        if not isinstance(action, str) or action not in ("approve", "reject", "answer"):
            raise BoardError("action must be approve, reject or answer")
        post = await writer.write(
            "reply", post_id, action, text=opt_str(d, "text"), request_id=opt_str(d, "request_id"),
            expect_gen=opt_str(d, "expect_gen"),
        )
        hub.nudge.set()
        return post

    @app.post("/api/assets")
    async def upload_asset(request: Request):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_ASSET + 1024 * 1024:
            raise BoardError("image is larger than 20 MB", 413)
        try:
            form = await request.form()
        except Exception as e:
            raise BoardError(f"expected multipart/form-data with a 'file' field ({e})")
        f = form.get("file")
        if f is None or isinstance(f, str):
            raise BoardError("multipart field 'file' is required")
        rid = form.get("request_id")
        rid = rid if isinstance(rid, str) and rid else request.headers.get("x-request-id")
        data = await f.read()
        await form.close()
        info = await writer.write("add_asset", data, request_id=rid or None)
        return JSONResponse(info, status_code=201)

    @app.get("/api/posts")
    async def list_posts(agent: str | None = None, limit: str = "200"):
        try:
            n = int(limit)
        except ValueError:
            raise BoardError("limit must be an integer")
        posts, seq = await reader.call("list_posts", agent=agent or None, limit=n)
        return JSONResponse({"posts": posts, "seq": seq, "epoch": reader.store.epoch},
                            headers={"X-Board-Data": str(reader.store.dir)})

    @app.get("/api/posts/{post_id}")
    async def get_post(post_id: str):
        return await reader.call("get_post", post_id)

    @app.get("/api/assets/{asset_id}")
    async def get_asset(asset_id: str):
        path, mime = await reader.call("get_asset", asset_id)
        if not path.is_file():
            raise BoardError(f"asset file missing for {asset_id}", 404)
        return FileResponse(path, media_type=mime, headers=ASSET_HEADERS)

    # ------------------------------------------------------------------ SSE

    @app.get("/api/events")
    async def events(request: Request, since: str | None = None):
        raw = request.headers.get("last-event-id") or since
        resume, epoch = None, reader.store.epoch
        if raw not in (None, ""):
            m = RESUME_RE.match(raw.strip())
            if not m:
                raise BoardError("Last-Event-ID / since must be SEQ or EPOCH:SEQ (ASCII digits)")
            resume = int(m.group(2))
            if m.group(1) is not None and m.group(1) != epoch:
                resume = -1  # another database (wiped, replaced, other --data): reset
        sub = hub.subscribe()  # FIRST, so nothing committed from now on can be missed

        async def stream() -> AsyncIterator[bytes]:
            loop = asyncio.get_running_loop()
            try:
                current = await reader.call("current_seq")
                yield b"retry: 1000\n\n" + sse_frame("hello", {"seq": current, "epoch": epoch})
                last = current if resume is None else resume
                if resume is not None:
                    lowest = await reader.call("min_event_id")
                    if resume > current or resume < 0 or (lowest and resume < lowest - 1):
                        yield sse_frame("reset", {"seq": current, "epoch": epoch})
                        last = current
                while not hub.stopping.is_set():  # replay what the client missed, in batches
                    batch = await reader.call("events_after", last, REPLAY_BATCH, BATCH_BYTES)
                    if not batch:
                        break
                    for e in batch:
                        if hub.stopping.is_set():
                            return
                        last = e["id"]
                        yield hub.frame(e)
                next_ping = loop.time() + PING_INTERVAL
                while not sub.closed or not sub.queue.empty():  # live
                    if loop.time() >= next_ping:  # on a timer, busy or not: detects dead peers
                        yield b": ping\n\n"
                        next_ping = loop.time() + PING_INTERVAL
                    try:
                        item = await asyncio.wait_for(sub.queue.get(), max(0.0, next_ping - loop.time()))
                    except asyncio.TimeoutError:
                        continue
                    item = sub.get_nowait_frame(item)
                    if item is None:  # shutdown or dropped as too slow: client reconnects
                        return
                    eid, frame = item
                    if eid <= last:  # already sent during replay
                        continue
                    last = eid
                    yield frame
            except asyncio.CancelledError:  # server shutting down mid-stream: end quietly
                return
            finally:
                hub.unsubscribe(sub)
                sub.drop()

        return SSEResponse(stream(), hub.stopping, media_type="text/event-stream", headers=SSE_HEADERS)

    # ------------------------------------------------------------------ page

    @app.get("/", include_in_schema=False)
    async def index():
        page = STATIC_DIR / "index.html"
        if page.is_file():
            return FileResponse(page, media_type="text/html", headers={"Cache-Control": "no-cache"})
        return HTMLResponse(PLACEHOLDER)

    app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")
    return app


def run(host: str = "127.0.0.1", port: int = 8000, data_dir: Path | str | None = None) -> None:
    import uvicorn

    uvicorn.run(
        create_app(data_dir),
        host=host,
        port=port,
        workers=1,
        timeout_graceful_shutdown=2,
        log_level="warning",
    )
