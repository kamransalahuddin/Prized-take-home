"""The agent-facing `board` CLI. Spec: docs/contract.md section 2; user text: board/help.py.

Writes go straight to SQLite through `Store` (LocalBackend) unless BOARD_URL is set, in
which case the same calls go over HTTP (HttpBackend). httpx / fastapi / uvicorn are
imported lazily so the common path (`board post`) starts fast.
"""

from __future__ import annotations

import codecs
import errno
import json
import os
import re
import select
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, Callable, List, Optional

import typer

from board.help import HELP
from board.store import (
    MAX_TITLE,
    BoardError,
    Store,
    StoreBusy,
    default_data_dir,
    env_seconds,
    loads_strict,
    normalize_agent,
    normalize_key,
    normalize_payload,
    normalize_status,
    normalize_table,
    proc_start,
    strip_ansi,
)

FLUSH_INTERVAL = 0.25
FINISH_PATIENCE = env_seconds("BOARD_FINISH_PATIENCE", 60)  # retry final chunk while board is down
FLUSH_BYTES = 256 * 1024
MAX_PENDING = 10 * 1024 * 1024
MAX_CHUNK = 256 * 1024  # characters per streamed append (small events; well under the 8 MB body limit)
HINT = "Run 'board help' for usage."
# One-shot HTTP writes retry transient failures (connection errors, timeouts, 5xx) with the
# SAME request_id for this long: a slow request may still commit, and the retry then returns
# the stored result instead of writing twice.
WRITE_PATIENCE = env_seconds("BOARD_WRITE_PATIENCE", 20)
MIN_ATTEMPTS = 4  # ... and at least this many tries (unless that takes 3x the patience)
READ_PATIENCE = 5.0  # `board ls` over HTTP: a read can just be run again
CONNECT_TIMEOUT = 3.0  # HTTP: no connection within this -> the server is not reachable
READ_TIMEOUT = 30.0  # ... but a slow answer is waited for (the write may be committing)
CREATE_WAIT = 3.0  # `board log` starts the command after this even if the post isn't created yet
INTERRUPT_PATIENCE = 10.0  # posting the rest of the output after a SIGINT/SIGTERM/SIGHUP
KILL_GRACE = 5.0  # after forwarding a stop signal, SIGKILL the child's group if still running
GROUP_GRACE = 1.0  # after a stopped child exits: TERM its leftover group, KILL after this
DRAIN_AFTER_EXIT = 1.0  # read the pipe this long after the child exits (grandchildren may hold it)
STOP_SIGNALS = tuple(getattr(signal, n) for n in ("SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT") if hasattr(signal, n))
WAIT_POLL_LOCAL = 0.5
WAIT_POLL_HTTP = 1.0
SERVER_ERROR_PATIENCE = env_seconds("BOARD_SERVER_ERROR_PATIENCE", 30)  # wait: give up on 5xx
EXIT_TIMEOUT = 4
POST_ID_RE = re.compile(r"^p_[a-z2-7]{4,16}$")
REAP_EVERY = 60.0  # direct mode: close orphaned live logs at most this often (any command)


def new_request_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------- errors


class InputError(Exception):
    """Bad command or input: exit 2."""


class Transient(Exception):
    """An HTTP request may or may not have been applied (connection error, timeout, 5xx).
    Safe to retry with the same request_id: the server de-duplicates."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


class Unreachable(Transient):
    """BOARD_URL set but the server is not reachable / did not answer: exit 3."""


class ServerError(Transient):
    """The server answered 5xx (or 408/429): exit 1 once retries are exhausted."""

    def __init__(self, message: str, status: int | None = None, request_id: str | None = None):
        super().__init__(message, status)
        self.request_id = request_id


class MaybeApplied(Transient):
    """A write reached the server but no answer came back (read timeout, connection dropped
    after sending): it may have been applied. Exit 1 once retries are exhausted."""

    def __init__(self, url: str, cause: str, request_id: str | None):
        super().__init__(f"BOARD_URL {url} did not answer ({cause}); the write may already have been "
                         f"applied (request id {request_id}); check with board ls before repeating it")
        self.request_id = request_id
        self.short = f"BOARD_URL {url} did not answer ({cause})"


def is_transient(e: BaseException) -> bool:
    """Worth retrying with the same request_id: HTTP transient failures and a locked/busy
    local database. Everything else (rejections, disk full, read-only, corrupt) is final."""
    if isinstance(e, (Transient, StoreBusy)):
        return True
    if isinstance(e, sqlite3.OperationalError):
        m = str(e).lower()
        return "locked" in m or "busy" in m
    return False


def err_text(e: BaseException) -> str:
    if isinstance(e, OSError) and e.strerror:
        return f"{e.strerror}: {e.filename}" if e.filename else e.strerror
    return getattr(e, "message", None) or str(e) or type(e).__name__


def warn(msg: str) -> None:
    try:
        sys.stderr.write(f"board: {msg}\n")
        sys.stderr.flush()
    except (OSError, ValueError, AttributeError):
        pass


def clean(s: str | None) -> str | None:
    """Make a command-line / environment string valid UTF-8 (invalid bytes arrive as lone
    surrogates and would crash later); bad bytes become U+FFFD."""
    if s is None:
        return None
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        pass
    try:
        return s.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except UnicodeEncodeError:
        return s.encode("utf-8", "replace").decode("utf-8")


# ---------------------------------------------------------------- backends


class LocalBackend:
    def __init__(self, data_dir: Path | None = None, read_only: bool = False):
        d = data_dir or default_data_dir()
        db = d.expanduser() / "board.db"
        # A read command on a database this user cannot write still works (read-only).
        ro = read_only and db.is_file() and not (os.access(db, os.W_OK) and os.access(db.parent, os.W_OK))
        self.store = Store(d, readonly=ro)

    def create_post(self, patience: float | None = None, **kw) -> tuple[dict, bool]:
        return self.store.create_post(**kw)

    def append(self, post_id: str, patience: float | None = None, **kw) -> dict:
        return self.store.append(post_id, **kw)

    def add_asset(self, data: bytes, request_id: str | None = None) -> dict:
        return self.store.add_asset(data, request_id=request_id)

    def reply(self, post_id: str, action: str, text: str | None = None, request_id: str | None = None) -> dict:
        return self.store.reply(post_id, action, text, request_id=request_id)

    def get_post(self, post_id: str, patience: float | None = None) -> dict:
        return self.store.get_post(post_id)

    def list_posts(self, agent: str | None, limit: int) -> list[dict]:
        posts, _ = self.store.list_posts(agent, limit, with_payload=False)
        # ls falls back to the text for untitled posts; fetch payloads only for those.
        for i, p in enumerate(posts):
            if not p["title"] and p["kind"] in ("text", "markdown", "log"):
                posts[i] = self.store.get_post(p["id"])
        return posts

    def maybe_reap(self) -> None:
        """Close live logs whose `board log` died without reporting (kill -9) even when no
        server runs; at most once a minute across all processes, never failing the command."""
        if self.store.readonly:
            return
        marker = self.store.dir / ".reaped"
        try:
            if time.time() - marker.stat().st_mtime < REAP_EVERY:
                return
        except OSError:
            pass
        try:
            marker.touch()
            self.store.reap_dead_logs()
        except Exception:
            pass


class HttpBackend:
    def __init__(self, url: str):
        import httpx  # lazy: only when BOARD_URL is set

        self.httpx = httpx
        self.url = url.rstrip("/")
        self.client = httpx.Client(base_url=self.url,
                                   timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT))

    def _unreachable(self, e: BaseException) -> Unreachable:
        return Unreachable(f"cannot reach BOARD_URL {self.url}: {e or type(e).__name__}. "
                           "Start board serve there, or unset BOARD_URL to save posts locally.")

    def _req(self, method: str, path: str, request_id: str | None = None, **kw) -> Any:
        httpx = self.httpx
        try:
            r = self.client.request(method, path, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol) as e:
            raise self._unreachable(e) from e  # never sent: nothing was applied
        except httpx.TransportError as e:
            if method == "POST":  # sent (at least partly), no answer: may have been applied
                raise MaybeApplied(self.url, str(e) or type(e).__name__, request_id) from e
            raise Unreachable(f"BOARD_URL {self.url} did not answer: {e or type(e).__name__}") from e
        if r.status_code >= 400:
            try:
                msg = r.json().get("error") or r.text
            except (ValueError, AttributeError):
                msg = r.text.strip()[:200] or r.reason_phrase
            if r.status_code >= 500 or r.status_code in (408, 429):
                raise ServerError(f"{msg} (HTTP {r.status_code})", r.status_code,
                                  request_id if method == "POST" else None)
            raise BoardError(f"{msg} (HTTP {r.status_code})", r.status_code)
        try:
            return r.json()
        except ValueError as e:
            raise BoardError(f"BOARD_URL {self.url} did not answer like a board server", 502) from e

    def _retrying(self, patience: float | None, fn: Callable[[], Any], request_id: str | None = None) -> Any:
        """Call fn, retrying transient failures (with backoff) until `patience` seconds have
        passed (and, with patience > 0, at least MIN_ATTEMPTS tries: a request that timed out
        may still have committed, so slow attempts must not use up all the tries); then
        re-raise the last one. Writes keep their request_id on every retry. If any attempt
        of a write reached the server, the final error says it may have been applied."""
        patience = WRITE_PATIENCE if patience is None else patience
        start, delay, attempts, reached = time.monotonic(), 0.25, 0, None
        while True:
            try:
                attempts += 1
                return fn()
            except Transient as e:
                if isinstance(e, MaybeApplied):
                    reached = e
                elapsed = time.monotonic() - start
                if elapsed >= patience and (patience <= 0 or attempts >= MIN_ATTEMPTS or elapsed >= 3 * patience):
                    if reached is not None and isinstance(e, Unreachable):
                        raise reached from e  # down now, but an earlier try got through
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 2.0)

    def _write(self, path: str, body: dict | None = None, patience: float | None = None,
               request_id: str | None = None, **kw) -> Any:
        if body is not None:
            kw["json"] = body
            request_id = request_id or body.get("request_id")
        return self._retrying(patience, lambda: self._req("POST", path, request_id, **kw), request_id)

    def create_post(self, patience: float | None = None, **kw) -> tuple[dict, bool]:
        body = {k: v for k, v in kw.items() if v is not None}
        body.setdefault("request_id", new_request_id())
        post = self._write("/api/posts", body, patience)
        created = post.pop("created", True)
        return post, created

    def append(self, post_id: str, patience: float | None = None, **kw) -> dict:
        body = {k: v for k, v in kw.items() if v is not None and v is not False}
        body.setdefault("request_id", new_request_id())
        return self._write(f"/api/posts/{post_id}/append", body, patience)

    def reply(self, post_id: str, action: str, text: str | None = None, request_id: str | None = None) -> dict:
        body = {"action": action, "request_id": request_id or new_request_id()}
        if text is not None:
            body["text"] = text
        return self._write(f"/api/posts/{post_id}/reply", body)

    def add_asset(self, data: bytes, request_id: str | None = None) -> dict:
        rid = request_id or new_request_id()
        return self._write("/api/assets", files={"file": ("image", data)}, data={"request_id": rid},
                           headers={"X-Request-Id": rid}, request_id=rid)

    def get_post(self, post_id: str, patience: float | None = None) -> dict:
        return self._retrying(patience, lambda: self._req("GET", f"/api/posts/{post_id}"))

    def list_posts(self, agent: str | None, limit: int) -> list[dict]:
        params: dict = {"limit": limit}
        if agent:
            params["agent"] = agent
        return self._retrying(READ_PATIENCE, lambda: self._req("GET", "/api/posts", params=params))["posts"]

    def maybe_reap(self) -> None:
        pass  # the server reaps


def backend(read_only: bool = False):
    url = os.environ.get("BOARD_URL", "").strip()
    return HttpBackend(url) if url else LocalBackend(read_only=read_only)


# ---------------------------------------------------------------- input helpers

FILE_EXTS = {".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".svg", ".png", ".jpg", ".jpeg", ".gif",
             ".webp", ".log", ".html", ".yaml", ".yml", ".xml", ".out"}


def stdin_is_tty() -> bool:
    try:
        return sys.stdin is None or sys.stdin.isatty()
    except (ValueError, OSError):
        return True


def read_stdin_bytes() -> bytes:
    if stdin_is_tty():
        raise InputError("nothing on stdin")
    data = sys.stdin.buffer.read()
    if not data:
        raise InputError("nothing on stdin")
    return data


def is_file(path: str) -> bool:
    try:
        return Path(path).expanduser().is_file()
    except (OSError, ValueError):
        return False


def read_file_bytes(path: str) -> bytes:
    if not is_file(path):
        p = Path(path).expanduser()
        try:
            exists = p.exists()
        except (OSError, ValueError):
            exists = False
        raise InputError(f"not a regular file: {path}" if exists else f"file not found: {path}")
    try:
        return Path(path).expanduser().read_bytes()
    except OSError as e:
        raise InputError(f"cannot read {path}: {e.strerror}") from e


def read_value(v: str) -> str:
    """Value rule: literal, `-` = all of stdin, `@path` = file contents. `@text` that names
    no file is literal, unless it clearly looks like a path (has a / or a file extension)."""
    if v == "-":
        return read_stdin_bytes().decode("utf-8", "replace")
    if v.startswith("@") and len(v) > 1:
        path = v[1:]
        if not is_file(path) and path.strip() != path and is_file(path.strip()):
            path = path.strip()  # "@ notes/x.md": the space after @ is not part of the name
        if is_file(path):
            return read_file_bytes(path).decode("utf-8", "replace")
        if looks_like_path(path.strip()):
            raise InputError(f"file not found: {path.strip()} (to post text starting with @, pipe it in with -)")
    return v


def looks_like_path(s: str) -> bool:
    """`@notes/report.md` is a file reference; `@human please approve`, `@alice.smith`
    and `@v1.2` are text."""
    if any(c.isspace() for c in s):
        return False
    return "/" in s or s.startswith("~") or Path(s).suffix.lower() in FILE_EXTS


def agent_name(agent: str | None) -> str:
    try:
        return normalize_agent(agent or clean(os.environ.get("BOARD_AGENT")))
    except BoardError as e:
        raise InputError(e.message) from e


def status_name(status: str | None) -> str | None:
    if status is None:
        return None
    try:
        return normalize_status(status)
    except BoardError as e:
        raise InputError(e.message) from e


def key_name(key: str | None) -> str | None:
    try:
        return normalize_key(key)
    except BoardError as e:
        raise InputError(e.message) from e


def title_text(title: str | None) -> str | None:
    if title is not None and len(title.strip()) > MAX_TITLE:
        warn(f"the title is longer than {MAX_TITLE} characters; it was cut")
    return title


def post_id_arg(post_id: str) -> str:
    if not POST_ID_RE.match(post_id):
        raise InputError(f"bad post ID {post_id!r}: IDs look like p_3fak2a7q (printed by board post)")
    return post_id


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def out(line: str) -> None:
    """Write a line to stdout. A closed or broken stdout never fails the command."""
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        pass


def emit(obj: dict, as_json: bool, quiet: bool) -> None:
    if quiet:
        return
    out(dumps(obj) if as_json else obj["id"])


# ---------------------------------------------------------------- streaming engine


class Streamer:
    """Echo bytes to stderr and ship them to a post in batches (every 250 ms or 256 KB).

    With `create` (create_post kwargs including a fixed request_id) the post is created
    by the flusher thread, retrying transient failures with the SAME request_id while the
    output is buffered: a create that commits late is still the post that receives the
    output. `created` is set once the post exists (or creating it failed for good).

    Transient failures (HTTP down / 5xx / timeouts, a locked database) are retried with the
    text kept in memory (capped at 10 MB); they never stop the echo or the producer. A
    permanent failure (rejected, post replaced -> expect_gen 409, disk full) stops shipping
    but keeps echoing.

    Each chunk gets its own request_id and keeps it (and its exact text) across retries:
    if a send committed but its response was lost, the retry is recognised and stored
    once. New output waits in `pending` and is never merged into a chunk being retried.
    """

    def __init__(self, be, post_id: str | None, echo: bool, expect_kind: str | None = None,
                 expect_gen: str | None = None, create: dict | None = None,
                 on_created: Callable[[dict], None] | None = None, continuation: bool = False,
                 opener: Callable[["Streamer"], Any] | None = None,
                 open_failed: Callable[[BaseException], str] | None = None):
        self.be = be
        # opener(streamer) -> backend, run first in the flusher thread: opening the store
        # (which may wait up to 30 s on a lock) never delays the command or the stdin drain.
        self.opener = opener
        self.open_failed = open_failed or (lambda e: f"{err_text(e)}; not posting the output")
        self.open_error: BaseException | None = None
        # text/markdown streams: only the first chunk is joined with a separator; later
        # chunks continue it verbatim (append raw=True), wherever the flushes fall.
        self.continuation = continuation
        self.started = False
        self.expect_kind = expect_kind
        self.expect_gen = expect_gen
        self.post_id = post_id
        self.echo = echo
        self.create_kw = create
        self.on_created = on_created
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.pending = ""
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.abort = threading.Event()  # stop retrying in finish() (a signal arrived)
        self.created = threading.Event()
        self.dead = (be is None and opener is None) or (post_id is None and create is None)
        if post_id is not None or self.dead:
            self.created.set()
        self.error: BaseException | None = None  # why posting stopped / never started
        self.last_error: BaseException | None = None
        self.warned = False
        self.dropped = False
        self.inflight: tuple[str, str, dict] | None = None  # (request_id, text, kw) awaiting success
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def feed(self, data: bytes) -> None:
        if self.echo:
            try:
                sys.stderr.buffer.write(data)
                sys.stderr.buffer.flush()
            except (OSError, ValueError, AttributeError):
                self.echo = False
        if self.dead:
            return
        text = self.decoder.decode(data)
        if not text:
            return
        with self.lock:
            self.pending += text
            if len(self.pending) > MAX_PENDING:
                self.pending = self.pending[-MAX_PENDING:]
                self.dropped = True
            big = len(self.pending) >= FLUSH_BYTES
        if big:
            self.wake.set()

    def _take(self, final: bool) -> str:
        """Take what is ready to send (at most MAX_CHUNK unless final), holding back a
        trailing partial ANSI escape or a lone trailing \\r (it may be the first half of
        \\r\\n) until more data arrives."""
        with self.lock:
            s = self.pending
            if final:
                s += self.decoder.decode(b"", final=True)
                self.pending = ""
            else:
                cut = min(len(s), MAX_CHUNK)
                esc = s.rfind("\x1b", 0, cut)
                if esc != -1 and cut - esc < 64 and strip_ansi(s[esc:cut]).startswith("\x1b"):
                    cut = esc
                if cut and s[cut - 1] == "\r":
                    cut -= 1
                s, self.pending = s[:cut], s[cut:]
        return strip_ansi(s)

    def _giveback(self, text: str) -> None:
        with self.lock:
            self.pending = text + self.pending
            if len(self.pending) > MAX_PENDING:
                self.pending = self.pending[-MAX_PENDING:]
                self.dropped = True

    def unposted(self) -> int:
        with self.lock:
            return len(self.pending) + (len(self.inflight[1]) if self.inflight else 0)

    def _retry_later(self, e: BaseException) -> bool:
        self.last_error = e
        if not self.warned:
            warn(f"could not post output to the board (will retry): {getattr(e, 'short', None) or err_text(e)}")
            self.warned = True
        return False

    def _fail(self, e: BaseException, what: str) -> bool:
        self.dead, self.error = True, e
        self.created.set()
        warn(f"{what}: {err_text(e)}")
        return True

    def _create(self) -> bool:
        """Try to create the post once. True when done (created, or failed for good)."""
        try:
            post, created = self.be.create_post(patience=0, **self.create_kw)
        except Exception as e:
            if is_transient(e):
                return self._retry_later(e)
            return self._fail(e, "cannot create the log post; output is not posted")
        self.post_id, self.expect_gen = post["id"], post.get("gen")
        self.warned = False
        post["created"] = created
        self.created.set()
        if self.on_created:
            self.on_created(post)
        return True

    def _send(self, rid: str, text: str, **kw) -> bool:
        """One append; True when done (sent, or failed for good). On a transient failure
        the caller retries the same (rid, text)."""
        try:
            self.be.append(self.post_id, text=text or None, expect_kind=self.expect_kind,
                           expect_gen=self.expect_gen, request_id=rid, patience=0, **kw)
            self.warned = False
            return True
        except Exception as e:
            if is_transient(e):
                return self._retry_later(e)
            return self._fail(e, f"stopped posting output to {self.post_id}")

    def _chunk_kw(self, text: str) -> dict:
        """Fixed when the chunk is made (it is part of the chunk's identity on retries)."""
        if not text:
            return {}
        kw = {"raw": True} if self.continuation and self.started else {}
        self.started = True
        return kw

    def _step(self) -> bool:
        if self.post_id is None:
            return self._create()
        if self.inflight is None:
            text = self._take(final=False)
            if not text:
                return True
            self.inflight = (new_request_id(), text, self._chunk_kw(text))
        rid, text, kw = self.inflight
        if self._send(rid, text, **kw):
            self.inflight = None
            return True
        return False

    def _open(self) -> bool:
        try:
            self.be = self.opener(self)
            return True
        except Exception as e:
            self.dead, self.error, self.open_error = True, e, e
            self.created.set()
            warn(self.open_failed(e))
            return False

    def _run(self) -> None:
        if self.opener is not None and not self._open():
            return
        backoff = 0.0  # create the post at once
        while True:
            if backoff:
                self.wake.wait(backoff)
                self.wake.clear()
            if self.stopping.is_set() or self.dead:
                break
            ok = self._step()
            backoff = FLUSH_INTERVAL if ok else min(max(backoff, FLUSH_INTERVAL) * 2, 5.0)

    def finish(self, patience: float = FINISH_PATIENCE, **final_kw) -> None:
        """Stop the flusher and send the rest together with the final status change,
        retrying transient failures for up to `patience` s (or until `abort` is set)."""
        self.stopping.set()
        self.wake.set()
        self.thread.join()
        if self.dead:
            return
        deadline, delay = time.monotonic() + patience, 0.25
        told = False
        what = "board unreachable" if isinstance(self.be, HttpBackend) else "board database busy"

        def until(fn: Callable[[], bool]) -> bool:
            nonlocal delay, told
            while not fn():
                if self.abort.is_set() or time.monotonic() + delay > deadline:
                    return False
                if not told:
                    warn(f"{what}; retrying for up to {int(patience)}s to post the end of the output")
                    told = True
                self.abort.wait(delay)
                delay = min(delay * 2, 5.0)
            return True

        if self.post_id is None:
            if not until(self._create):
                self.error = self.error or self.last_error
                left = len(self._take(final=True).encode("utf-8", "replace"))
                warn(f"could not create the log post on the board; {left} bytes of output were not posted"
                     + (f" ({err_text(self.last_error)})" if self.last_error else ""))
                return
            if self.dead:
                return
        text = self._take(final=True)
        if self.dropped:
            text = "[board: output was dropped while the board was unreachable]\n" + text
        chunks: list[tuple[str, str, dict]] = []
        if self.inflight is not None:  # never merged with newer text: it keeps its id
            chunks.append(self.inflight)
        pieces = [text[i:i + MAX_CHUNK] for i in range(0, len(text), MAX_CHUNK)]
        for i, piece in enumerate(pieces):
            chunks.append((new_request_id(), piece,
                           {**self._chunk_kw(piece), **(final_kw if i == len(pieces) - 1 else {})}))
        if not pieces and final_kw:
            chunks.append((new_request_id(), "", final_kw))
        for n, (rid, chunk, kw) in enumerate(chunks):
            if not until(lambda: self._send(rid, chunk, **kw)):
                left = sum(len(c[1].encode("utf-8", "replace")) for c in chunks[n:])
                warn(f"giving up: {left} bytes of output not posted to {self.post_id}")
                return
            if self.dead:
                return
            self.inflight = None


def pump(fd: int, streamer: Streamer, should_stop: Callable[[], bool] = lambda: False) -> None:
    """Copy fd into the streamer until EOF or should_stop() (checked every 0.2 s)."""
    while not should_stop():
        try:
            ready, _, _ = select.select([fd], [], [], 0.2)
        except InterruptedError:
            continue
        except (OSError, ValueError):
            break
        if not ready:
            continue
        try:
            data = os.read(fd, 65536)
        except InterruptedError:
            continue
        except OSError:
            break
        if not data:
            break
        streamer.feed(data)


KEEP_SIGNAL_HANDLERS = False  # set by main(): the process exits right after the command


class StopSignals:
    """Handle SIGINT/SIGTERM/SIGHUP/SIGQUIT while board streams: remember them and call
    `on_signal(signum)`. On exit the old handlers are restored, except in the real CLI
    process (KEEP_SIGNAL_HANDLERS): there a late signal in a storm must not kill board
    with the default action between the command finishing and the exit (exit 128+N of
    the first signal, always)."""

    def __init__(self, on_signal: Callable[[int], None]):
        self.on_signal = on_signal
        self.got: list[int] = []
        self.old: dict[int, Any] = {}

    def _handler(self, signum, _frame) -> None:
        self.got.append(signum)
        self.on_signal(signum)

    def __enter__(self) -> "StopSignals":
        for sig in STOP_SIGNALS:
            try:
                self.old[sig] = signal.signal(sig, self._handler)
            except (ValueError, OSError):  # not the main thread
                pass
        return self

    def __exit__(self, *exc) -> None:
        if KEEP_SIGNAL_HANDLERS:
            return
        for sig, h in self.old.items():
            try:
                signal.signal(sig, h)
            except (ValueError, OSError, TypeError):
                pass


def stream_input(fd: int, streamer: Streamer) -> int | None:
    """Pipe mode / append -: pump fd into the streamer. On a stop signal keep draining for
    up to 1 s, then stop. Returns the first signal received, or None."""
    stop_at: list[float] = []

    def on_signal(signum: int) -> None:
        if not stop_at:
            stop_at.append(time.monotonic() + DRAIN_AFTER_EXIT)
        elif streamer.stopping.is_set():
            streamer.abort.set()

    with StopSignals(on_signal) as sigs:
        pump(fd, streamer, lambda: bool(stop_at) and time.monotonic() > stop_at[0])
        return sigs.got[0] if sigs.got else None


# ---------------------------------------------------------------- app

app = typer.Typer(
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": []},
    no_args_is_help=False,
)

AgentOpt = Annotated[Optional[str], typer.Option("-a", "--agent")]
TitleOpt = Annotated[Optional[str], typer.Option("-t", "--title")]
StatusOpt = Annotated[Optional[str], typer.Option("-s", "--status")]
KeyOpt = Annotated[Optional[str], typer.Option("-k", "--key")]
JsonOpt = Annotated[bool, typer.Option("--json")]
QuietOpt = Annotated[bool, typer.Option("-q", "--quiet")]


@app.command("help")
def help_cmd() -> None:
    out(HELP.rstrip("\n"))


def check_payload(kind: str, payload: dict) -> None:
    """Validate with the store's own rules before writing or sending, so a bad value is
    exit 2 in both modes (never a traceback while encoding the HTTP body)."""
    try:
        normalize_payload(kind, payload)
    except BoardError as e:
        if e.status == 413:
            raise  # too large: rejected (exit 1)
        raise InputError(e.message) from e


def parse_chart(raw: str) -> dict:
    try:
        opt = loads_strict(raw, "chart")
    except BoardError as e:
        raise InputError(e.message) from e
    if not isinstance(opt, dict):
        raise InputError('chart must be a JSON object (an ECharts option with "series")')
    if not any(k in opt for k in ("series", "baseOption", "options")):
        raise InputError('chart must be an ECharts option object with "series"')
    return opt


def parse_table(raw: str) -> dict:
    try:
        return normalize_table(raw)
    except BoardError as e:
        if e.status == 413:
            raise  # too large: rejected (exit 1), like an oversized image
        raise InputError(e.message) from e


@app.command("post")
def post_cmd(
    text: Annotated[Optional[str], typer.Argument()] = None,
    agent: AgentOpt = None,
    title: TitleOpt = None,
    status: StatusOpt = None,
    key: KeyOpt = None,
    md: Annotated[Optional[str], typer.Option("--md", "--markdown")] = None,
    table: Annotated[Optional[str], typer.Option("--table")] = None,
    chart: Annotated[Optional[str], typer.Option("--chart")] = None,
    image: Annotated[Optional[str], typer.Option("--image")] = None,
    text_opt: Annotated[Optional[str], typer.Option("--text")] = None,
    as_json: JsonOpt = False,
    quiet: QuietOpt = False,
) -> None:
    sources = [v for v in (text, text_opt, md, table, chart, image) if v is not None]
    if len(sources) != 1:
        raise InputError("use exactly one of TEXT, --md, --table, --chart, --image")
    a = agent_name(agent)
    s = status_name(status)
    k = key_name(key)
    title = title_text(title)
    if image is not None:
        data = read_stdin_bytes() if image == "-" else read_file_bytes(image)
        be = backend()
        asset = be.add_asset(data)
        kind, payload = "image", {"asset_id": asset["id"]}
    else:
        if chart is not None:
            kind, payload = "chart", {"option": parse_chart(read_value(chart))}
        elif table is not None:
            kind, payload = "table", parse_table(read_value(table))
        elif md is not None:
            kind, payload = "markdown", {"markdown": read_value(md)}
        else:
            kind, payload = "text", {"text": read_value(sources[0])}
        check_payload(kind, payload)
        be = backend()
    post, created = be.create_post(agent=a, kind=kind, payload=payload, title=title, status=s, key=k)
    post["created"] = created
    emit(post, as_json, quiet)
    be.maybe_reap()


def end_group(pgid: int) -> None:
    """After a stop signal: the child exited, but members of its process group may live on
    (`sh -c 'sleep 100 & wait'`: background jobs ignore SIGINT/SIGQUIT). TERM the group,
    then KILL it if anything is still there after GROUP_GRACE s."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return  # nothing left
    deadline = time.monotonic() + GROUP_GRACE
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except OSError:
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def run_child(cmd: List[str], streamer: Streamer) -> int:
    """Run CMD in its own process group with output into the streamer; return its exit
    code (128+N if killed by signal N). Stop signals received by board are forwarded to
    the whole group exactly once; if the child is still running KILL_GRACE s after one,
    the group is killed; once a stopped child exits, the rest of its group is ended too.
    Returns once the child itself exits (after draining the pipe for
    up to DRAIN_AFTER_EXIT s), even if background grandchildren keep the pipe open."""
    state: dict[str, Any] = {"proc": None, "pending": [], "kill_at": None, "exited": False}

    def forward(signum: int) -> None:
        try:
            os.killpg(state["proc"].pid, signum)
        except OSError:
            pass
        if state["kill_at"] is None:
            state["kill_at"] = time.monotonic() + KILL_GRACE

    def on_signal(signum: int) -> None:
        if state["exited"]:  # child gone: don't touch background leftovers; stop retrying
            streamer.abort.set()
        elif state["proc"] is None:
            state["pending"].append(signum)  # replayed right after spawn
        else:
            forward(signum)

    with StopSignals(on_signal) as sigs:
        if not streamer.created.wait(CREATE_WAIT) and not sigs.got:
            warn("the board is slow to answer; running the command anyway "
                 "(the log post ID is printed once it exists)")
        if sigs.got:  # stopped before the command started: don't start it
            rc = 128 + sigs.got[0]
            streamer.finish(patience=INTERRUPT_PATIENCE, done=True, exit_code=rc)
            return rc
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        # CMD has no stdin or terminal: make git fail fast instead of waiting on an editor
        # or a password prompt nobody can answer.
        env.setdefault("GIT_EDITOR", "true")
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=env, start_new_session=True)
        except OSError as e:
            rc = 127 if isinstance(e, FileNotFoundError) else 126
            msg = f"cannot run {cmd[0]}: {e.strerror or e}"
            warn(msg)
            if not streamer.dead:
                streamer._giveback(f"board: {msg}\n")
            streamer.finish(done=True, exit_code=rc)
            return rc
        state["proc"] = proc
        for signum in state.pop("pending"):
            forward(signum)
        state["pending"] = []
        stop_reading = threading.Event()
        reader = threading.Thread(target=pump, args=(proc.stdout.fileno(), streamer, stop_reading.is_set),
                                  daemon=True)
        reader.start()
        killed = False
        while True:
            try:
                rc = proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if state["kill_at"] is not None and time.monotonic() > state["kill_at"]:
                    warn(f"{cmd[0]} did not stop within {KILL_GRACE:g}s; killing it")
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
                    state["kill_at"], killed = float("inf"), True
        state["exited"] = True
        if sigs.got:  # stopped: also end what it left running in its group
            end_group(proc.pid)
        reader.join(DRAIN_AFTER_EXIT)
        if reader.is_alive():  # a background grandchild holds the pipe: stop reading
            stop_reading.set()
            reader.join(1.0)
        if rc < 0:
            rc = 128 - rc
        if killed and sigs.got:
            rc = 128 + sigs.got[0]
        streamer.finish(done=True, exit_code=rc)
        return rc


@app.command("log", context_settings={"allow_extra_args": True})
def log_cmd(
    ctx: typer.Context,
    agent: AgentOpt = None,
    title: TitleOpt = None,
    status: StatusOpt = None,
    key: KeyOpt = None,
    as_json: JsonOpt = False,
    quiet: QuietOpt = False,
) -> None:
    cmd: List[str] = list(ctx.args)
    a = agent_name(agent)
    s = status_name(status)
    k = key_name(key)
    title = title_text(title)
    if not cmd and stdin_is_tty():
        raise InputError("pipe output into board log, or use: board log -a NAME -- CMD")
    if cmd and not title:
        title = shlex.join(cmd)
    # Never break the producer: whatever goes wrong with the board, the command still
    # runs (or stdin is still drained) and its output is still echoed.
    owner: dict[str, Any] = {"pid": os.getpid()}
    try:
        import socket

        owner["host"] = socket.gethostname()
        start = proc_start(os.getpid())
        if start:
            owner["pstart"] = start
    except Exception:
        pass
    create = dict(agent=a, kind="log", payload=owner, title=title, status=s, key=k, live=True,
                  request_id=new_request_id())
    doing = "running the command" if cmd else "passing output through"
    streamer = Streamer(None, None, echo=not quiet, expect_kind="log", create=create,
                        on_created=lambda post: emit(post, as_json, quiet),
                        opener=lambda _st: backend(),
                        open_failed=lambda e: f"{err_text(e)}; {doing} without posting")
    streamer.start()

    if not cmd:  # pipe mode
        streamer.created.wait(CREATE_WAIT)
        got = stream_input(sys.stdin.fileno(), streamer)
        if got is not None:
            with StopSignals(lambda _s: streamer.abort.set()):
                streamer.finish(patience=INTERRUPT_PATIENCE, done=True, status="error")
            raise typer.Exit(128 + got)
        with StopSignals(lambda _s: streamer.abort.set()):
            streamer.finish(done=True)
        if streamer.post_id is None and streamer.error is not None:
            raise streamer.error
        if streamer.dead:  # posting stopped (warned): exit 1, like append -
            raise typer.Exit(1)
        return

    rc = run_child(cmd, streamer)
    raise typer.Exit(rc)


@app.command("append")
def append_cmd(
    post_id: Annotated[Optional[str], typer.Argument(metavar="ID")] = None,
    text: Annotated[Optional[str], typer.Argument()] = None,
    status: StatusOpt = None,
    title: TitleOpt = None,
    as_json: JsonOpt = False,
    quiet: QuietOpt = False,
) -> None:
    if not post_id:
        raise InputError("give the post ID: board append ID TEXT (or -s STATUS / -t TITLE)")
    post_id_arg(post_id)
    s = status_name(status)
    title = title_text(title)
    if text == "":
        text = None
    if text is None and s is None and title is None:
        raise InputError("give TEXT to add, or -s STATUS / -t TITLE to change")
    if text == "-":
        if stdin_is_tty():
            raise InputError("nothing on stdin")
        append_stream(post_id, s, title, as_json, quiet)
        return
    value = read_value(text) if text is not None else None
    res = backend().append(post_id, text=value, status=s, title=title, newline=value is not None)
    emit(res, as_json, quiet)


def append_stream(post_id: str, s: str | None, title: str | None, as_json: bool, quiet: bool) -> None:
    """`board append ID -`: stream stdin into the post. Board problems never stop the
    drain/echo; they make the exit code 1 (3 if BOARD_URL is unreachable) at the end."""
    def opener(st: Streamer):  # in the flusher thread: stdin is drained meanwhile
        be = backend()
        post = be.get_post(post_id)
        if post["kind"] not in ("log", "text", "markdown"):
            raise BoardError("can only append to log, text or markdown posts; use board post -k KEY to replace it", 409)
        st.expect_kind, st.expect_gen = post["kind"], post.get("gen")
        st.continuation = post["kind"] in ("text", "markdown")
        return be

    streamer = Streamer(None, post_id, echo=not quiet, opener=opener,
                        open_failed=lambda e: f"{err_text(e)}; passing output through without posting")
    streamer.start()
    got = stream_input(sys.stdin.fileno(), streamer)
    with StopSignals(lambda _s: streamer.abort.set()):
        streamer.stopping.set()  # (the post's kind is known once the opener finished)
        streamer.wake.set()
        streamer.thread.join()
        final = {k: v for k, v in (("status", s), ("title", title)) if v is not None}
        if got is not None and streamer.expect_kind == "log":
            final = {"done": True, "status": "error"}
        streamer.finish(patience=INTERRUPT_PATIENCE if got is not None else FINISH_PATIENCE, **final)
    if got is not None:
        raise typer.Exit(128 + got)
    if streamer.open_error is not None:
        raise streamer.open_error
    if streamer.dead:
        raise typer.Exit(1)
    be = streamer.be
    res = be.get_post(post_id)
    res.pop("payload", None)
    emit(res, as_json, quiet)


# ---------------------------------------------------------------- ask / wait


TimeoutOpt = Annotated[Optional[float], typer.Option("--timeout", min=0)]


def print_reply(post: dict, as_json: bool, quiet: bool = False) -> None:
    if quiet:
        return
    rep = post["payload"]["reply"]
    if as_json:
        out(dumps({"id": post["id"], **rep}))
        return
    out(rep["action"])
    if rep.get("text"):
        out(rep["text"].rstrip("\n"))


def wait_for_reply(be, post_id: str, timeout: float | None, as_json: bool, post: dict | None = None,
                   quiet: bool = False) -> None:
    """Poll until the post has a reply, then print it. Direct mode reads the store every
    0.5 s; BOARD_URL mode asks the server every 1 s and keeps waiting while it is down.
    Remembers the question's gen: if the question is replaced (re-asked with --key), this
    waiter stops instead of printing an answer to a different question."""
    remote = isinstance(be, HttpBackend)
    interval = WAIT_POLL_HTTP if remote else WAIT_POLL_LOCAL
    deadline = None if timeout is None else time.monotonic() + timeout
    warned = False
    first = post is None
    gen = post.get("gen") if post else None
    failing_since: float | None = None
    try:
        while True:
            if post is None:
                try:
                    post = be.get_post(post_id, patience=0)
                    failing_since = None
                except Exception as e:
                    if not is_transient(e):
                        raise
                    if isinstance(e, ServerError):  # a 502 blip is fine; a server stuck on 500s is not
                        failing_since = failing_since or time.monotonic()
                        if time.monotonic() - failing_since > SERVER_ERROR_PATIENCE:
                            raise
                    if not warned:
                        hint = f"Still waiting for the reply on {post_id}."
                        warn(f"{err_text(e)} {hint}" if isinstance(e, Unreachable)
                             else f"{err_text(e)}; still waiting for the reply on {post_id}")
                        warned = True
            if post is not None:
                if gen is None:
                    gen = post.get("gen")
                elif post.get("gen") not in (None, gen):
                    raise BoardError(f"your question was replaced by a newer one (board wait {post_id})")
                if post["payload"].get("reply"):
                    print_reply(post, as_json, quiet)
                    return
                if post["status"] != "blocked":
                    if first:
                        raise BoardError("post is not waiting for input")
                    raise BoardError(f"post {post_id} is no longer waiting for input "
                                     f"(status changed to {post['status']} without a reply)")
                first = False
                post = None
            if deadline is not None and time.monotonic() >= deadline:
                warn(f"no reply yet on {post_id}; the question stays open. "
                     f"Run 'board wait {post_id}' later to get the answer.")
                raise typer.Exit(EXIT_TIMEOUT)
            pause = interval if deadline is None else max(0.0, min(interval, deadline - time.monotonic()))
            time.sleep(pause)
    except KeyboardInterrupt:
        warn(f"stopped waiting; the question stays open. Run 'board wait {post_id}' to resume.")
        raise typer.Exit(130)


@app.command("ask")
def ask_cmd(
    question: Annotated[Optional[str], typer.Argument()] = None,
    agent: AgentOpt = None,
    title: TitleOpt = None,
    key: KeyOpt = None,
    timeout: TimeoutOpt = None,
    as_json: JsonOpt = False,
    quiet: QuietOpt = False,
) -> None:
    if question is None:
        raise InputError('give the question: board ask -a NAME "Can I delete the users table?"')
    a = agent_name(agent)
    k = key_name(key)
    title = title_text(title)
    text = read_value(question)
    if not text.strip():
        raise InputError("the question is empty")
    be = backend()
    post, _ = be.create_post(agent=a, kind="text", payload={"text": text, "ask": True}, title=title,
                             status="blocked", key=k)
    pid = post["id"]
    warn(f"waiting for the human on {pid} (board wait {pid} to resume)")
    wait_for_reply(be, pid, timeout, as_json, post=post, quiet=quiet)


@app.command("wait")
def wait_cmd(
    post_id: Annotated[Optional[str], typer.Argument(metavar="ID")] = None,
    timeout: TimeoutOpt = None,
    as_json: JsonOpt = False,
    quiet: QuietOpt = False,
) -> None:
    if not post_id:
        raise InputError("give the post ID: board wait ID")
    post_id_arg(post_id)
    wait_for_reply(backend(), post_id, timeout, as_json, quiet=quiet)


@app.command("ls")
def ls_cmd(
    agent: AgentOpt = None,
    limit: Annotated[int, typer.Option("-n", "--limit", min=1)] = 20,
    as_json: JsonOpt = False,
    quiet: QuietOpt = False,
) -> None:
    if agent is not None:
        a = agent.strip().rstrip("/")
        if not a:
            raise InputError(f"bad agent name {agent!r}: use lowercase letters, digits, . _ - and / "
                             "for subagents (e.g. lead/tests)")
        agent = agent_name(a)
    be = backend(read_only=True)
    posts = be.list_posts(agent, limit)
    be.maybe_reap()
    if quiet:
        return
    if as_json:
        for p in posts:
            p.pop("payload", None)
        out(dumps(posts))
        return
    for p in posts:
        label = p.get("title")
        if not label:
            pl = p.get("payload") or {}
            label = (pl.get("text") or pl.get("markdown") or "").strip()[:60]
        label = " ".join((label or "").split()) or "-"
        # Never let posted text drive the terminal (escape sequences, bells).
        label = "".join(c for c in label if c >= " " and not ("\x7f" <= c <= "\x9f")) or "-"
        out("\t".join([p["id"], p["agent"], p["kind"], p["status"], label]))


@app.command("serve")
def serve_cmd(
    port: Annotated[int, typer.Option("--port", "-p", min=1, max=65535)] = 8000,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    data: Annotated[Optional[Path], typer.Option("--data")] = None,
    as_json: JsonOpt = False,
    quiet: QuietOpt = False,
) -> None:
    ensure_std_fds()
    import httpx

    data_dir = (data or default_data_dir()).expanduser()
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{host}:{port}"
    r = None
    try:
        r = httpx.get(f"http://{probe_host}:{port}/api/posts", params={"limit": 1}, timeout=1.0)
        r = r if r.status_code == 200 and "posts" in r.json() else None
    except Exception:
        r = None
    if r is not None:
        theirs = r.headers.get("x-board-data")
        if theirs and Path(theirs).resolve() != data_dir.resolve():
            raise BoardError(f"a board for {theirs} is already running at {url}; "
                             f"use another --port to serve {data_dir}")
        warn(f"already running at {url}")
        serve_out(url, data_dir, as_json, quiet)
        return

    import socket

    try:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            # Same option uvicorn uses: lingering TIME_WAIT / FIN_WAIT connections from a
            # previous server must not block a restart; a live listener still fails.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
    except socket.gaierror as e:
        raise BoardError(f"cannot listen on {host}:{port}: unknown host name ({e.strerror or e}). "
                         "Use an address such as 127.0.0.1 or 0.0.0.0 with --host.") from e
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            hint = "Another program is using it; pick another with --port N."
        elif e.errno == errno.EADDRNOTAVAIL:
            hint = "That address is not on this machine; use 127.0.0.1 or 0.0.0.0 with --host."
        elif e.errno == errno.EACCES:
            hint = "Not allowed (ports below 1024 need root); pick another with --port N."
        else:
            hint = "Pick another --port N or --host."
        raise BoardError(f"cannot listen on {host}:{port}: {e.strerror or e}. {hint}") from e

    import uvicorn

    from board.server import create_app

    application = create_app(data_dir)
    warn(f"serving {url}  data: {data_dir}")
    if host in ("0.0.0.0", "::"):
        warn(f"listening on {host} with no auth: anyone who can reach this port can read and post. "
             "Prefer an SSH tunnel (ssh -L 8000:localhost:8000 host).")
    serve_out(url, data_dir, as_json, quiet)

    import uvicorn.server

    # SIGHUP (terminal closed) and SIGQUIT shut down cleanly too (WAL checkpoint), exit 0.
    extra = tuple(getattr(signal, n) for n in ("SIGHUP", "SIGQUIT") if hasattr(signal, n))
    uvicorn.server.HANDLED_SIGNALS = tuple(dict.fromkeys(uvicorn.server.HANDLED_SIGNALS + extra))

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame):  # INT/TERM/HUP/QUIT all shut down cleanly, exit 0
            super().handle_exit(sig, frame)
            self._captured_signals.clear()  # (uvicorn would re-raise the signal afterwards)

    config = uvicorn.Config(application, host=host, port=port, workers=1, timeout_graceful_shutdown=2,
                            log_level="warning", backlog=2048)  # a burst of SSE reconnects
    Server(config).run()


def ensure_std_fds() -> None:
    """`board serve >&-`: give closed stdout/stderr a /dev/null so uvicorn's logging (and
    our own prints) work instead of crashing on a missing stream."""
    for fd, name in ((1, "stdout"), (2, "stderr")):
        try:
            os.fstat(fd)
            ok = True
        except OSError:
            ok = False
            try:
                null = os.open(os.devnull, os.O_WRONLY)
                if null != fd:
                    os.dup2(null, fd)
                    os.close(null)
            except OSError:
                continue
        if not ok or getattr(sys, name) is None:
            try:
                setattr(sys, name, open(fd, "w", buffering=1, closefd=False))
            except OSError:
                pass


def serve_out(url: str, data_dir: Path, as_json: bool, quiet: bool) -> None:
    if quiet:
        return
    out(dumps({"url": url, "data": str(data_dir)}) if as_json else url)


# ---------------------------------------------------------------- entry point

VALUE_OPTS = {
    "post": {"-a", "--agent", "-t", "--title", "-s", "--status", "-k", "--key",
             "--md", "--markdown", "--table", "--chart", "--image", "--text"},
    "append": {"-s", "--status", "-t", "--title"},
    "ask": {"-a", "--agent", "-t", "--title", "-k", "--key", "--timeout"},
}
# Options that take a value, per command: the token after them is a value, never -h.
HELP_VALUE_OPTS = dict(
    VALUE_OPTS,
    log={"-a", "--agent", "-t", "--title", "-s", "--status", "-k", "--key"},
    wait={"--timeout"},
    ls={"-a", "--agent", "-n", "--limit"},
    serve={"--port", "-p", "--host", "--data"},
)
_OPTION_LIKE = re.compile(r"^--?[A-Za-z][A-Za-z0-9-]*(=.*)?$")


def protect_dash_text(args: list[str]) -> list[str]:
    """Let `board post -a x "-3 tests failed"` and `board append ID "- item"` work:
    a positional value that starts with '-' but is not option-shaped (or contains spaces)
    is moved after `--` so the parser treats it as text, not an unknown option."""
    if not args or args[0] not in VALUE_OPTS:
        return args
    takes_value = VALUE_OPTS[args[0]]
    opts, pos, i = [args[0]], [], 1
    while i < len(args):
        t = args[i]
        if t == "--":
            pos += args[i + 1:]
            break
        if t in takes_value and i + 1 < len(args):
            opts += [t, args[i + 1]]
            i += 2
            continue
        if t.startswith("-") and t != "-" and not _OPTION_LIKE.match(t):
            pos.append(t)
        elif t.startswith("-") and t != "-":
            opts.append(t)
        else:
            pos.append(t)
        i += 1
    if not any(p.startswith("-") and p != "-" for p in pos):
        return args
    return opts + ["--"] + pos


def wants_help(args: list[str]) -> bool:
    """Bare `board`, `board help`, or a standalone -h/--help before `--` (not the value
    of an option, as in `board post -a x -t -h text`)."""
    if not args or args[0] == "help":
        return True
    takes_value = HELP_VALUE_OPTS.get(args[0], set())
    skip = False
    for t in args:
        if t == "--":
            break
        if skip:
            skip = False
        elif t in takes_value:
            skip = True
        elif t in ("-h", "--help"):
            return True
    return False


def run(args: list[str]) -> int:
    args = [clean(a) for a in args]
    if wants_help(args):
        out(HELP.rstrip("\n"))
        return 0
    try:
        command = typer.main.get_command(app)
        rv = command.main(protect_dash_text(args), prog_name="board", standalone_mode=False)
        return rv if isinstance(rv, int) else 0
    except InputError as e:
        warn(str(e))
        warn_hint()
        return 2
    except BoardError as e:
        warn(e.message)
        return 1
    except Unreachable as e:
        warn(e.message)
        return 3
    except ServerError as e:
        hint = (f"; the write may already have been applied (request id {e.request_id}): check with "
                "board ls before repeating it") if e.request_id else ""
        warn(f"the board server failed: {e.message}{hint}")
        return 1
    except MaybeApplied as e:
        warn(e.message)
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    except sqlite3.Error as e:
        warn(f"board database error: {e} (data: {default_data_dir()})")
        return 1
    except OSError as e:
        warn(err_text(e))
        return 2 if e.errno in (errno.ENAMETOOLONG, errno.ENOTDIR) else 1
    except Exception as e:
        # Click's exceptions: typer >= 0.20 vendors click (typer._click), older typer uses
        # click itself, so match by class name instead of importing either.
        names = {c.__name__ for c in type(e).__mro__}
        if "Exit" in names:
            return getattr(e, "exit_code", 0)
        if "UsageError" in names:
            warn(e.format_message())
            warn_hint()
            return 2
        if "Abort" in names:
            return 130
        raise


def warn_hint() -> None:
    try:
        sys.stderr.write(HINT + "\n")
    except (OSError, ValueError, AttributeError):
        pass


def main() -> None:
    global KEEP_SIGNAL_HANDLERS
    KEEP_SIGNAL_HANDLERS = True
    code = run(sys.argv[1:])
    try:
        sys.stdout.flush()
    except (BrokenPipeError, OSError, ValueError, AttributeError):
        # Don't let a closed stdout turn into a traceback at interpreter shutdown.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
        except OSError:
            pass
    sys.exit(code)


if __name__ == "__main__":
    main()
