"""Durable storage shared by the CLI (direct writes) and the server (reads + HTTP writes).

SQLite in WAL mode plus an assets/ directory under the data dir. Every mutation writes
its row changes AND an `events` row in one IMMEDIATE transaction, so a committed event
always describes committed state. `events.id` is the global, monotonically increasing
`seq`; each post records the seq of the last event that touched it.

Safe for many concurrent writer processes (busy_timeout + short transactions).
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KINDS = ("text", "markdown", "table", "chart", "image", "log")
STATUSES = ("info", "ok", "warn", "error", "blocked")
APPENDABLE = ("log", "text", "markdown")
AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*$")
LOG_CAP = 2 * 1024 * 1024  # keep the last 2 MB of each log
MAX_ASSET = 20 * 1024 * 1024
MAX_TABLE_ROWS = 50_000
MAX_REPLY = 10_000
REPLY_ACTIONS = ("approve", "reject", "answer")
REQUEST_TTL = 24 * 3600  # idempotency records older than this are pruned
MAX_PAYLOAD = 8 * 1024 * 1024  # serialized payload cap (same as the HTTP body limit)
MAX_KEY = 100
MAX_TITLE = 200
LOCK_PATIENCE = float(os.environ.get("BOARD_LOCK_PATIENCE", 30))  # total wait for a locked DB
EVENT_KEEP = 20_000  # events kept for SSE replay; older clients get `reset` (refetch)
SCHEMA_VERSION = 2
_B32 = "abcdefghijklmnopqrstuvwxyz234567"

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  id         TEXT PRIMARY KEY,
  agent      TEXT NOT NULL,
  kind       TEXT NOT NULL,
  title      TEXT,
  status     TEXT NOT NULL DEFAULT 'info',
  key        TEXT,
  payload    TEXT NOT NULL,          -- JSON; for logs the text lives in log_chunks
  live       INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  seq        INTEGER NOT NULL DEFAULT 0,
  gen        TEXT                    -- new random token on every create and --key replace
);
CREATE UNIQUE INDEX IF NOT EXISTS posts_agent_key ON posts(agent, key) WHERE key IS NOT NULL;
CREATE INDEX IF NOT EXISTS posts_updated ON posts(updated_at);
CREATE TABLE IF NOT EXISTS log_chunks (
  post_id    TEXT NOT NULL,
  n          INTEGER NOT NULL,
  text       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (post_id, n)
);
CREATE TABLE IF NOT EXISTS assets (
  id         TEXT PRIMARY KEY,
  filename   TEXT NOT NULL,
  mime       TEXT NOT NULL,
  size       INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  type       TEXT NOT NULL,          -- 'post' | 'append'
  post_id    TEXT NOT NULL,
  data       TEXT NOT NULL,          -- JSON event payload (see docs/contract.md)
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (   -- idempotency: request_id -> stored result
  id         TEXT PRIMARY KEY,
  result     TEXT NOT NULL,          -- JSON {"op": ..., "result": ...}
  created_at TEXT NOT NULL,
  fp         TEXT                    -- sha256 of op + normalized arguments
);
CREATE INDEX IF NOT EXISTS requests_created ON requests(created_at);
CREATE TABLE IF NOT EXISTS meta (       -- epoch: random id of this database (SSE resume check)
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL
);
INSERT OR IGNORE INTO meta(key, value) VALUES ('epoch', lower(hex(randomblob(4))));
"""


class BoardError(Exception):
    """Validation / lookup failure. `status` is the HTTP status the server should use."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class StoreBusy(BoardError):
    """The database stayed locked by another process for LOCK_PATIENCE seconds. Transient:
    a retry later can succeed (HTTP 503)."""

    def __init__(self, message: str):
        super().__init__(message, 503)


class StoreUnavailable(BoardError):
    """The data directory was removed or replaced under a running server (HTTP 503)."""

    def __init__(self, message: str):
        super().__init__(message, 503)


def _reject_constant(name: str):
    raise ValueError(f"{name} is not valid JSON (use null or a number)")


def loads_strict(s: str, what: str) -> Any:
    """json.loads that rejects NaN/Infinity and absurd nesting with a BoardError."""
    try:
        return json.loads(s, parse_constant=_reject_constant)
    except json.JSONDecodeError as e:
        raise BoardError(f"{what}: invalid JSON at line {e.lineno}, col {e.colno}: {e.msg}")
    except RecursionError:
        raise BoardError(f"{what}: JSON is nested too deeply")
    except ValueError as e:
        raise BoardError(f"{what}: {e}")


def dumps_checked(obj: Any, what: str = "payload", limit: int | None = MAX_PAYLOAD) -> str:
    """Serialize for storage: no NaN/Infinity, valid UTF-8 (no lone surrogates), not too
    deep, at most `limit` bytes. Raises BoardError (400 / 413)."""
    try:
        s = json.dumps(obj, ensure_ascii=False, allow_nan=False)
        n = len(s.encode("utf-8"))
    except UnicodeEncodeError:  # (a ValueError subclass: must come first)
        raise BoardError(f"{what}: text is not valid UTF-8 (lone surrogate)")
    except ValueError as e:
        raise BoardError(f"{what}: NaN and Infinity are not allowed (use null): {e}")
    except RecursionError:
        raise BoardError(f"{what}: nested too deeply")
    if limit is not None and n > limit:
        raise BoardError(f"{what} is larger than {limit // (1024 * 1024)} MB", 413)
    return s


def check_text(value: str | None, what: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BoardError(f"{what} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise BoardError(f"{what}: text is not valid UTF-8 (lone surrogate)")
    return value


def normalize_key(key: Any) -> str | None:
    if key is None or key == "":
        return None
    check_text(key, "key")
    k = key.strip()
    if not k:
        raise BoardError("key is empty (only spaces)")
    if len(k) > MAX_KEY:
        raise BoardError(f"key is longer than {MAX_KEY} characters")
    return k


def now_iso() -> str:
    d = datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def iso_at(ts: float) -> str:
    d = datetime.fromtimestamp(ts, timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def check_request_id(request_id: Any) -> str | None:
    if request_id is None or request_id == "":
        return None
    if not isinstance(request_id, str) or len(request_id) > 200:
        raise BoardError("request_id must be a string of at most 200 characters")
    return check_text(request_id, "request_id")


def proc_start(pid: int) -> str | None:
    """Start time of a process (to tell a reused pid apart), or None if unknown."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]  # field 22: starttime
    except (OSError, IndexError):
        pass
    import subprocess

    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
        return " ".join(r.stdout.split()) or None
    except (OSError, subprocess.SubprocessError):
        return None


def new_gen() -> str:
    return secrets.token_hex(8)


def fingerprint(op: str, args: Any) -> str:
    """Identity of a write request: same request_id must mean the same request."""
    raw = json.dumps([op, args], sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()


def new_id(prefix: str, n: int) -> str:
    return prefix + "".join(secrets.choice(_B32) for _ in range(n))


def default_data_dir() -> Path:
    return Path(os.environ.get("BOARD_DIR") or os.environ.get("BOARD_DATA") or Path.home() / ".board").expanduser()


# ---------------------------------------------------------------- validation helpers


def normalize_agent(agent: str | None) -> str:
    if agent is not None and not isinstance(agent, str):
        raise BoardError("agent must be a string")
    a = (agent or "").strip().lower()
    if not a:
        raise BoardError("-a/--agent is required (e.g. -a lead, or -a lead/tests for a subagent)")
    if len(a) > 64 or not AGENT_RE.match(a):
        raise BoardError(
            f"bad agent name {agent!r}: use lowercase letters, digits, . _ - and / for subagents (e.g. lead/tests)"
        )
    return a


def normalize_status(status: str | None) -> str:
    s = (status or "info").strip().lower()
    if s not in STATUSES:
        raise BoardError(f"bad status {status!r}: use one of {', '.join(STATUSES)}")
    return s


def sniff_image(data: bytes) -> str:
    """Return the MIME type of an allowed image, or raise BoardError(415)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 33 or data[12:16] != b"IHDR":
            raise BoardError("not an image: PNG is truncated (no IHDR header)", 415)
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        if len(data) < 32:
            raise BoardError("not an image: JPEG is truncated", 415)
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        if len(data) < 26:
            raise BoardError("not an image: GIF is truncated", 415)
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if len(data) < 30 or data[12:15] != b"VP8":
            raise BoardError("not an image: WebP is truncated", 415)
        return "image/webp"
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith(b"<svg") or (head.startswith((b"<?xml", b"<!--", b"<!doctype svg")) and b"<svg" in head):
        return "image/svg+xml"
    raise BoardError("not an image: use PNG, JPEG, GIF, WebP or SVG", 415)


def _cell(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return json.dumps(v, ensure_ascii=False)


def normalize_table(value: Any) -> dict:
    """Accept {columns, rows}, a JSON array of objects, a JSON array of arrays (row 0 =
    header), or CSV/TSV text. Return {"columns": [...], "rows": [[...], ...]}."""
    if isinstance(value, str):
        s = value.strip()
        if not s:
            raise BoardError("table is empty")
        s = s.lstrip("\ufeff")
        if s[:1] in ("[", "{"):
            value = loads_strict(s, "table")
        else:
            try:
                dialect = csv.Sniffer().sniff(s[:4096], delimiters=",\t;")
            except csv.Error:
                dialect = csv.excel
            try:
                rows = [r for r in csv.reader(io.StringIO(s, newline=""), dialect) if r]
            except csv.Error as e:
                raise BoardError(f"table: cannot read CSV: {e}")
            if len(rows) < 2:
                raise BoardError("table needs a header row and at least one data row "
                                 "(JSON array of objects, JSON array of arrays, or CSV)")
            value = rows
    if isinstance(value, dict) and "columns" in value and "rows" in value:
        if not isinstance(value["columns"], list) or not isinstance(value["rows"], list) or \
                not all(isinstance(r, list) for r in value["rows"]):
            raise BoardError('table: "columns" must be a list and "rows" a list of lists')
        cols = [str(c) for c in value["columns"]]
        rows = [[_cell(c) for c in r] for r in value["rows"]]
    elif isinstance(value, list) and value and all(isinstance(r, dict) for r in value):
        cols: list[str] = []
        for r in value:
            for k in r:
                if k not in cols:
                    cols.append(k)
        rows = [[_cell(r.get(c)) for c in cols] for r in value]
    elif isinstance(value, list) and value and all(isinstance(r, list) for r in value):
        cols = [str(c) for c in value[0]]
        rows = [[_cell(c) for c in r] for r in value[1:]]
    else:
        raise BoardError("table must be a JSON array of objects, a JSON array of arrays (first row = header), or CSV")
    if len(rows) > MAX_TABLE_ROWS:
        raise BoardError(f"table has {len(rows)} rows; the limit is {MAX_TABLE_ROWS}", 413)
    width = max([len(cols)] + [len(r) for r in rows])
    cols += [""] * (width - len(cols))
    rows = [r + [None] * (width - len(r)) for r in rows]
    return {"columns": cols, "rows": rows}


def normalize_payload(kind: str, payload: Any) -> dict:
    out = _normalize_payload(kind, payload)
    dumps_checked(out)  # NaN / lone surrogates / too deep / too large -> 400 / 413
    if payload.get("ask") is True and kind != "log":
        out["ask"] = True  # created by `board ask`: the page offers reply buttons
    return out


def _normalize_payload(kind: str, payload: Any) -> dict:
    if kind not in KINDS:
        raise BoardError(f"bad kind {kind!r}: use one of {', '.join(KINDS)}", 415)
    if not isinstance(payload, dict):
        raise BoardError("payload must be an object")
    if kind == "text":
        if not isinstance(payload.get("text"), str):
            raise BoardError("text post needs payload.text")
        return {"text": payload["text"]}
    if kind == "markdown":
        if not isinstance(payload.get("markdown"), str):
            raise BoardError("markdown post needs payload.markdown")
        return {"markdown": payload["markdown"]}
    if kind == "table":
        return normalize_table(payload if "columns" in payload else payload.get("table", payload))
    if kind == "chart":
        opt = payload.get("option")
        if not isinstance(opt, dict) or not ("series" in opt or "baseOption" in opt or "options" in opt):
            raise BoardError('chart must be an ECharts option object with "series"')
        return {"option": opt}
    if kind == "image":
        aid = payload.get("asset_id")
        if not isinstance(aid, str):
            raise BoardError("image post needs payload.asset_id (upload via POST /api/assets first)")
        return {"asset_id": aid}
    # log: pid/host identify the `board log` process so a server can close logs whose
    # writer died without reporting (kill -9); see Store.reap_dead_logs.
    ec = payload.get("exit_code")
    if ec is not None and (isinstance(ec, bool) or not isinstance(ec, int)):
        raise BoardError("exit_code must be an integer")
    out = {"exit_code": ec, "truncated": False}
    if isinstance(payload.get("pid"), int) and isinstance(payload.get("host"), str):
        out["pid"], out["host"] = payload["pid"], payload["host"][:255]
        if isinstance(payload.get("pstart"), str):
            out["pstart"] = payload["pstart"][:64]
    return out


# ---------------------------------------------------------------- store


class Store:
    def __init__(self, data_dir: str | Path | None = None):
        self.dir = Path(data_dir).expanduser() if data_dir else default_data_dir()
        self.assets_dir = self.dir / "assets"
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "board.db"
        self.conn = sqlite3.connect(self.db_path, timeout=LOCK_PATIENCE, isolation_level=None,
                                    check_same_thread=False)
        try:
            self.conn.row_factory = sqlite3.Row
            self.conn.execute(f"PRAGMA busy_timeout={int(LOCK_PATIENCE * 1000)}")
            # Switching a fresh DB to WAL can fail with "locked" when several processes open
            # it at once (busy_timeout is not always honored for this pragma), so retry it.
            self._retry(lambda: self.conn.execute("PRAGMA journal_mode=WAL"))
            self.conn.execute("PRAGMA synchronous=NORMAL")
            tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {"sqlite_sequence"} and "posts" not in tables:
                raise sqlite3.DatabaseError("not a database written by board (no posts table)")
            self._retry(lambda: self.conn.executescript(SCHEMA))
            self._migrate()
            self._validate_schema()
        except sqlite3.DatabaseError as e:
            self.conn.close()
            if isinstance(e, sqlite3.OperationalError) and not _not_a_db(e):
                raise
            raise BoardError(f"{self.db_path} is not a board database (or is from an incompatible "
                             f"version): {e}", 500) from e
        except BaseException:
            self.conn.close()
            raise
        self.epoch = self.conn.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]
        self._ident = self._file_ident(self.db_path)
        self._wal_ident = self._file_ident(Path(str(self.db_path) + "-wal"))

    # -- plumbing

    def _migrate(self) -> None:
        """Add columns that older databases lack (posts.gen, requests.fp)."""
        def cols(c, table):
            return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}

        if "gen" in cols(self.conn, "posts") and "fp" in cols(self.conn, "requests"):
            return

        def tx(c):
            if "gen" not in cols(c, "posts"):
                c.execute("ALTER TABLE posts ADD COLUMN gen TEXT")
                c.execute("UPDATE posts SET gen=lower(hex(randomblob(8))) WHERE gen IS NULL")
            if "fp" not in cols(c, "requests"):
                c.execute("ALTER TABLE requests ADD COLUMN fp TEXT")

        self._tx(tx)

    _COLUMNS = {
        "posts": {"id", "agent", "kind", "title", "status", "key", "payload", "live", "created_at",
                  "updated_at", "seq", "gen"},
        "log_chunks": {"post_id", "n", "text", "created_at"},
        "assets": {"id", "filename", "mime", "size", "created_at"},
        "events": {"id", "type", "post_id", "data", "created_at"},
        "requests": {"id", "result", "created_at", "fp"},
    }

    def _validate_schema(self) -> None:
        for table, want in self._COLUMNS.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if not want <= have:
                raise sqlite3.DatabaseError(f"table {table} lacks columns {sorted(want - have)}")
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            self._retry(lambda: self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}"))

    @staticmethod
    def _file_ident(path: Path) -> tuple[int, int] | None:
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_dev, st.st_ino)

    def check_same_file(self) -> None:
        """Raise StoreUnavailable if board.db (or its -wal) was removed or replaced since this
        connection opened it: writes would go to an unlinked file and be lost. Server-side."""
        wal = Path(str(self.db_path) + "-wal")
        if self._file_ident(self.db_path) != self._ident or (
                self._wal_ident is not None and self._file_ident(wal) != self._wal_ident):
            raise StoreUnavailable("board data directory was removed or replaced; restart board serve "
                                   f"(data: {self.dir})")

    def close(self) -> None:
        try:
            with contextlib.suppress(sqlite3.Error):
                self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        finally:
            self.conn.close()

    def _retry(self, fn):
        """Retry while the database is locked by another process, for LOCK_PATIENCE seconds
        in total (each attempt already waits busy_timeout); then StoreBusy."""
        deadline = time.monotonic() + LOCK_PATIENCE
        i = 0
        while True:
            try:
                return fn()
            except sqlite3.OperationalError as e:
                m = str(e).lower()
                if "locked" not in m and "busy" not in m:
                    raise
                if time.monotonic() >= deadline:
                    raise StoreBusy(f"database is locked by another process (waited {LOCK_PATIENCE:g}s): "
                                    f"{self.db_path}") from e
                i += 1
                time.sleep(min(0.02 * i, 0.5))

    def _tx(self, fn):
        def run():
            c = self.conn
            c.execute("BEGIN IMMEDIATE")
            try:
                out = fn(c)
                c.execute("COMMIT")
                return out
            except BaseException:
                if c.in_transaction:  # SQLite may already have rolled back (disk full, I/O error)
                    with contextlib.suppress(sqlite3.Error):
                        c.execute("ROLLBACK")
                raise

        return self._retry(run)

    def data_version(self) -> int:
        """Changes when ANY other connection commits; cheap to poll (server-side only)."""
        return self.conn.execute("PRAGMA data_version").fetchone()[0]

    def current_seq(self) -> int:
        row = self.conn.execute("SELECT MAX(id) FROM events").fetchone()
        return row[0] or 0

    # -- reading

    def _log_text(self, c: sqlite3.Connection, post_id: str) -> str:
        return "".join(r[0] for r in c.execute("SELECT text FROM log_chunks WHERE post_id=? ORDER BY n", (post_id,)))

    def _row_to_post(self, c: sqlite3.Connection, r: sqlite3.Row, with_payload: bool = True) -> dict:
        post = {
            "id": r["id"],
            "agent": r["agent"],
            "kind": r["kind"],
            "title": r["title"],
            "status": r["status"],
            "key": r["key"],
            "live": bool(r["live"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "seq": r["seq"],
            "gen": r["gen"],
        }
        if with_payload:
            payload = json.loads(r["payload"])
            if r["kind"] == "log":
                payload["text"] = self._log_text(c, r["id"])
            if r["kind"] == "image":
                payload["url"] = f"/api/assets/{payload['asset_id']}"
                a = c.execute("SELECT mime FROM assets WHERE id=?", (payload["asset_id"],)).fetchone()
                payload["mime"] = a[0] if a else None
            post["payload"] = payload
        return post

    def get_post(self, post_id: str, c: sqlite3.Connection | None = None) -> dict:
        c = c or self.conn
        r = c.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        if not r:
            raise BoardError(f"no post {post_id}", 404)
        return self._row_to_post(c, r)

    def list_posts(self, agent: str | None = None, limit: int = 200, with_payload: bool = True) -> tuple[list[dict], int]:
        """Most recently changed first (by seq, which is monotonic). `agent` is a prefix match on path segments (lead matches lead/*).
        Returns (posts, seq) read in one snapshot so seq is consistent with the posts."""
        limit = max(1, min(int(limit), 2000))
        c = self.conn
        c.execute("BEGIN")
        try:
            seq = self.current_seq()
            if agent:
                a = agent.strip().lower().rstrip("/")
                rows = c.execute(
                    "SELECT * FROM posts WHERE agent=? OR agent LIKE ? ESCAPE '\\' ORDER BY seq DESC LIMIT ?",
                    (a, a.replace("_", "\\_").replace("%", "\\%") + "/%", limit),
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM posts ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
            posts = [self._row_to_post(c, r, with_payload) for r in rows]
        finally:
            if c.in_transaction:
                with contextlib.suppress(sqlite3.Error):
                    c.execute("COMMIT")
        return posts, seq

    def events_after(self, seq: int, limit: int = 1000, max_bytes: int = 1024 * 1024) -> list[dict]:
        """[{id, type, post_id, data}] with id > seq, ascending, at most `limit` events and
        about `max_bytes` of event data (always at least one event). An empty list means
        there is nothing newer. `data` is the decoded event payload."""
        out: list[dict] = []
        total = 0
        for r in self.conn.execute(
            "SELECT id, type, post_id, data FROM events WHERE id>? ORDER BY id LIMIT ?", (seq, limit)
        ):
            if out and total + len(r[3]) > max_bytes:
                break
            total += len(r[3])
            out.append({"id": r[0], "type": r[1], "post_id": r[2], "data": json.loads(r[3])})
        return out

    def min_event_id(self) -> int:
        return self.conn.execute("SELECT MIN(id) FROM events").fetchone()[0] or 0

    # -- writing

    def _event(self, c: sqlite3.Connection, etype: str, post_id: str, data: dict) -> int:
        cur = c.execute(
            "INSERT INTO events(type, post_id, data, created_at) VALUES (?,?,?,?)",
            (etype, post_id, "{}", now_iso()),
        )
        seq = cur.lastrowid
        if seq % 1000 == 0:  # keep the events table bounded; replay older than this -> reset
            c.execute("DELETE FROM events WHERE id <= ?", (seq - EVENT_KEEP,))
        data["seq"] = seq
        c.execute("UPDATE events SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), seq))
        c.execute("UPDATE posts SET seq=? WHERE id=?", (seq, post_id))
        return seq

    def _stored(self, c: sqlite3.Connection, op: str, request_id: str | None, fp: str | None = None) -> Any:
        """The stored result of an earlier write with this request_id, or None. The same
        id with a different request (other op, post or arguments) is a 409."""
        if request_id is None:
            return None
        row = c.execute("SELECT result, fp FROM requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            return None
        stored = json.loads(row[0])
        if stored.get("op") != op or (fp is not None and row[1] is not None and row[1] != fp):
            raise BoardError("request_id was already used for a different request", 409)
        return stored

    def _idem(self, c: sqlite3.Connection, op: str, request_id: str | None, fn, fp: str | None = None) -> Any:
        """Run `fn(c)` once per request_id (inside the caller's transaction). A retry of a
        write that already committed (response lost) returns the stored result instead of
        writing again."""
        stored = self._stored(c, op, request_id, fp)
        if stored is not None:
            return stored["result"]
        out = fn(c)
        if request_id is not None:
            self._remember(c, op, request_id, out, fp)
        return out

    def _remember(self, c: sqlite3.Connection, op: str, request_id: str, result: Any, fp: str | None) -> None:
        c.execute(
            "INSERT INTO requests(id, result, created_at, fp) VALUES (?,?,?,?)",
            (request_id, json.dumps({"op": op, "result": result}, ensure_ascii=False), now_iso(), fp),
        )
        c.execute("DELETE FROM requests WHERE created_at < ?", (iso_at(time.time() - REQUEST_TTL),))

    def create_post(
        self,
        agent: str,
        kind: str,
        payload: dict,
        title: str | None = None,
        status: str | None = None,
        key: str | None = None,
        live: bool = False,
        request_id: str | None = None,
    ) -> tuple[dict, bool]:
        """Create a post, or with `key` replace this agent's existing post with that key
        (same id and created_at; kind/title/status/payload replaced; log chunks cleared).
        Returns (post, created). With `request_id`, a repeat of the same request returns
        the first result without writing."""
        request_id = check_request_id(request_id)
        agent = normalize_agent(agent)
        status = normalize_status(status)
        if not isinstance(kind, str):
            raise BoardError("kind must be a string")
        payload = normalize_payload(kind, payload)
        title = (check_text(title, "title") or "").strip()[:MAX_TITLE] or None
        key = normalize_key(key)
        if not isinstance(live, bool):
            raise BoardError("live must be true or false")
        if kind == "image":
            if not self.conn.execute("SELECT 1 FROM assets WHERE id=?", (payload["asset_id"],)).fetchone():
                raise BoardError(f"no asset {payload['asset_id']}", 404)

        def tx(c: sqlite3.Connection):
            ts = now_iso()
            existing = (
                c.execute("SELECT id FROM posts WHERE agent=? AND key=?", (agent, key)).fetchone() if key else None
            )
            if existing:
                pid, created = existing[0], False
                c.execute(
                    "UPDATE posts SET kind=?, title=?, status=?, payload=?, live=?, updated_at=?, gen=? WHERE id=?",
                    (kind, title, status, json.dumps(payload, ensure_ascii=False), int(live), ts, new_gen(), pid),
                )
                c.execute("DELETE FROM log_chunks WHERE post_id=?", (pid,))
            else:
                created = True
                for _ in range(20):  # random ids: retry on the (rare) collision
                    pid = new_id("p_", 8)
                    if not c.execute("SELECT 1 FROM posts WHERE id=?", (pid,)).fetchone():
                        break
                c.execute(
                    "INSERT INTO posts(id, agent, kind, title, status, key, payload, live, created_at, updated_at, gen)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, agent, kind, title, status, key, json.dumps(payload, ensure_ascii=False), int(live), ts, ts,
                     new_gen()),
                )
            post = self.get_post(pid, c)
            post["seq"] = self._event(c, "post", pid, dict(post))
            return [post, created]

        fp = fingerprint("create_post", [agent, kind, payload, title, status, key, bool(live)])

        def run(c):
            stored = self._stored(c, "create_post", request_id, fp)
            if stored is not None:  # a retry: re-read the post (only its id is stored)
                res = stored["result"]
                if isinstance(res, dict):
                    return [self.get_post(res["id"], c), res["created"]]
                return res  # older databases stored the whole post
            post, created = tx(c)
            if request_id is not None:
                self._remember(c, "create_post", request_id, {"id": post["id"], "created": created}, fp)
            return [post, created]

        post, created = self._tx(run)
        return post, created

    def append(
        self,
        post_id: str,
        text: str | None = None,
        status: str | None = None,
        title: str | None = None,
        done: bool = False,
        exit_code: int | None = None,
        newline: bool = False,
        expect_kind: str | None = None,
        request_id: str | None = None,
        expect_gen: str | None = None,
    ) -> dict:
        """Append text to a log/text/markdown post and/or change status/title/live.

        log: text added verbatim (newline=True adds a trailing \\n if missing — one-off appends).
        text: joined with \\n. markdown: joined with \\n\\n.
        exit_code set → payload.exit_code, status ok/error unless status given; done → live false.
        Emits an 'append' event when text was added, else a 'post' event (full post).
        Returns a light result {id, seq, status, title, live, updated_at, size}.
        With `request_id`, a repeat of the same request returns the first result without
        writing (so a retried log chunk is stored exactly once). `expect_gen`: 409 if the
        post was created again / replaced with --key since (its gen changed)."""
        request_id = check_request_id(request_id)
        check_text(text, "text")
        check_text(title, "title")
        if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
            raise BoardError("exit_code must be an integer")
        if text == "":
            text = None
        if text is None and status is None and title is None and not done and exit_code is None:
            raise BoardError("nothing to append: give text, a status or a title")
        if status is not None:
            status = normalize_status(status)
        if text is not None and "\r" in text:
            text = _collapse_cr(text)

        def tx(c: sqlite3.Connection):
            r = c.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
            if not r:
                raise BoardError(f"no post {post_id}", 404)
            kind = r["kind"]
            if expect_kind and kind != expect_kind:
                raise BoardError(f"post {post_id} is now a {kind} post (replaced with --key); not appending", 409)
            if expect_gen and r["gen"] != expect_gen:
                raise BoardError(f"post {post_id} was replaced (--key) by a newer post; not appending", 409)
            if exit_code is not None and kind != "log":
                raise BoardError("exit_code only applies to log posts", 409)
            payload = json.loads(r["payload"])
            new_status = status or r["status"]
            if exit_code is not None:
                payload["exit_code"] = exit_code
                if status is None:
                    new_status = "ok" if exit_code == 0 else "error"
            new_title = (title.strip()[:MAX_TITLE] or None) if title is not None else r["title"]
            live = 0 if done else r["live"]
            gen = r["gen"]
            if status == "blocked" and payload.get("reply"):
                # Re-opened after an answer: forget the old answer so the human can reply
                # again and `board wait` does not return the stale one.
                payload.pop("reply")
                gen = new_gen()
            ts = now_iso()
            chunk = None
            if text:
                if kind not in APPENDABLE:
                    raise BoardError("can only append to log, text or markdown posts; use --key to replace", 409)
                if kind == "log":
                    chunk = text + ("\n" if newline and not text.endswith("\n") else "")
                    n = c.execute("SELECT COALESCE(MAX(n), 0) + 1 FROM log_chunks WHERE post_id=?", (post_id,)).fetchone()[0]
                    c.execute(
                        "INSERT INTO log_chunks(post_id, n, text, created_at) VALUES (?,?,?,?)", (post_id, n, chunk, ts)
                    )
                    size = c.execute("SELECT SUM(LENGTH(text)) FROM log_chunks WHERE post_id=?", (post_id,)).fetchone()[0]
                    if size > LOG_CAP:
                        # Keep exactly the last LOG_CAP characters (code points), so a client
                        # that concatenates the chunks can reproduce the stored text.
                        payload["truncated"] = True
                        excess = size - LOG_CAP
                        for cn, ln in c.execute(
                            "SELECT n, LENGTH(text) FROM log_chunks WHERE post_id=? ORDER BY n", (post_id,)
                        ).fetchall():
                            if excess <= 0:
                                break
                            if ln <= excess:
                                c.execute("DELETE FROM log_chunks WHERE post_id=? AND n=?", (post_id, cn))
                                excess -= ln
                            else:
                                c.execute("UPDATE log_chunks SET text=substr(text, ?) WHERE post_id=? AND n=?",
                                          (excess + 1, post_id, cn))
                                excess = 0
                else:
                    field = "text" if kind == "text" else "markdown"
                    sep = "\n" if kind == "text" else "\n\n"
                    chunk = sep + text
                    payload[field] = payload[field] + chunk
            c.execute(
                "UPDATE posts SET payload=?, status=?, title=?, live=?, updated_at=?, gen=? WHERE id=?",
                (dumps_checked(payload, f"{kind} post"), new_status, new_title, live, ts, gen, post_id),
            )
            size = None
            if kind == "log":
                size = c.execute("SELECT COALESCE(SUM(LENGTH(text)),0) FROM log_chunks WHERE post_id=?", (post_id,)).fetchone()[0]
            if chunk is not None or gen == r["gen"]:
                # Text and/or status/title/live/exit_code changes travel as a small `append`
                # event ("text" may be ""): never re-send a whole (up to 2 MB) log.
                ev = {
                    "id": post_id,
                    "agent": r["agent"],
                    "kind": kind,
                    "text": chunk or "",
                    "status": new_status,
                    "title": new_title,
                    "live": bool(live),
                    "truncated": bool(payload.get("truncated")),
                    "exit_code": payload.get("exit_code"),
                    "updated_at": ts,
                }
                seq = self._event(c, "append", post_id, ev)
            else:  # re-opened question (reply cleared, new gen): the full post
                post = self.get_post(post_id, c)
                seq = self._event(c, "post", post_id, dict(post))
            return {"id": post_id, "seq": seq, "status": new_status, "title": new_title, "live": bool(live),
                    "updated_at": ts, "size": size}

        fp = fingerprint("append", [post_id, text, status, title, bool(done), exit_code, bool(newline),
                                    expect_kind, expect_gen])
        return self._tx(lambda c: self._idem(c, "append", request_id, tx, fp))

    def reply(self, post_id: str, action: str, text: str | None = None, request_id: str | None = None,
              expect_gen: str | None = None) -> dict:
        """The human's answer to a blocked post: approve / reject (optional note) or answer
        (text required). Sets payload.reply = {action, text, at}, status blocked -> info,
        emits a 'post' event. Only once per post, and only while it is blocked (409).
        `expect_gen` (the gen of the question the human saw): 409 if the question was
        replaced (re-asked with --key) since. Returns the full post."""
        request_id = check_request_id(request_id)
        if not isinstance(action, str) or action not in REPLY_ACTIONS:
            raise BoardError("action must be approve, reject or answer")
        if text is not None and not isinstance(text, str):
            raise BoardError("text must be a string")
        if text is not None and not text.strip():
            text = None
        if action == "answer" and text is None:
            raise BoardError("answer needs text")
        if text is not None and len(text) > MAX_REPLY:
            raise BoardError(f"reply text is longer than {MAX_REPLY} characters")

        def tx(c: sqlite3.Connection):
            r = c.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
            if not r:
                raise BoardError(f"no post {post_id}", 404)
            if expect_gen and r["gen"] != expect_gen:
                raise BoardError("the question was replaced by a newer one; reload and answer that", 409)
            payload = json.loads(r["payload"])
            if payload.get("reply"):
                raise BoardError("already answered", 409)
            if r["status"] != "blocked":
                raise BoardError("post is not waiting for input", 409)
            ts = now_iso()
            payload["reply"] = {"action": action, "text": text, "at": ts}
            c.execute(
                "UPDATE posts SET payload=?, status='info', updated_at=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), ts, post_id),
            )
            post = self.get_post(post_id, c)
            post["seq"] = self._event(c, "post", post_id, dict(post))
            return post

        fp = fingerprint("reply", [post_id, action, text, expect_gen])

        def run(c):
            stored = self._stored(c, "reply", request_id, fp)
            if stored is not None:
                res = stored["result"]
                return self.get_post(res["id"], c) if set(res) == {"id"} else res
            post = tx(c)
            if request_id is not None:
                self._remember(c, "reply", request_id, {"id": post_id}, fp)
            return post

        return self._tx(run)

    def reap_dead_logs(self) -> int:
        """Close live logs whose `board log` process (on this host) no longer exists.
        Returns how many were closed."""
        import socket

        host = socket.gethostname()
        closed = 0
        for pid_, payload in self.conn.execute("SELECT id, payload FROM posts WHERE kind='log' AND live=1").fetchall():
            p = json.loads(payload)
            if p.get("host") != host or not isinstance(p.get("pid"), int):
                continue  # another machine's process: don't guess
            alive = True
            try:
                os.kill(p["pid"], 0)
            except ProcessLookupError:
                alive = False
            except PermissionError:
                pass  # exists, owned by someone else
            if alive and p.get("pstart"):
                now = proc_start(p["pid"])
                alive = now is None or now == p["pstart"]  # different start time: pid was reused
            if alive:
                continue
            try:
                self.append(pid_, text="[board: the logging process ended without reporting; output may be incomplete]\n",
                            status="error", done=True)
                closed += 1
            except BoardError:
                pass
        return closed

    # -- assets

    def add_asset(self, data: bytes, request_id: str | None = None) -> dict:
        request_id = check_request_id(request_id)
        fp = fingerprint("add_asset", hashlib.sha256(data).hexdigest())
        stored = self._stored(self.conn, "add_asset", request_id, fp)
        if stored is not None:
            return stored["result"]
        if len(data) > MAX_ASSET:
            raise BoardError("image is larger than 20 MB", 413)
        mime = sniff_image(data)
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp", "image/svg+xml": "svg"}[mime]
        aid = new_id("a_", 8)
        filename = f"{aid}.{ext}"
        tmp = self.assets_dir / f".{filename}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.assets_dir / filename)

        def tx(c: sqlite3.Connection):
            c.execute(
                "INSERT INTO assets(id, filename, mime, size, created_at) VALUES (?,?,?,?,?)",
                (aid, filename, mime, len(data), now_iso()),
            )
            return {"id": aid, "mime": mime, "size": len(data), "url": f"/api/assets/{aid}"}

        info = self._tx(lambda c: self._idem(c, "add_asset", request_id, tx, fp))
        if info["id"] != aid:  # a concurrent duplicate of this request won the race
            try:
                (self.assets_dir / filename).unlink()
            except OSError:
                pass
        return info

    def sweep_orphan_assets(self, older_than: float = 3600) -> int:
        """Delete assets no image post references, older than `older_than` seconds (an
        upload whose post was never created). Returns how many were removed."""
        cutoff = iso_at(time.time() - older_than)
        rows = self.conn.execute(
            "SELECT id, filename FROM assets WHERE created_at < ? AND id NOT IN "
            "(SELECT json_extract(payload, '$.asset_id') FROM posts WHERE kind='image')", (cutoff,)
        ).fetchall()
        for aid, filename in rows:
            self._tx(lambda c: c.execute("DELETE FROM assets WHERE id=?", (aid,)))
            with contextlib.suppress(OSError):
                (self.assets_dir / filename).unlink()
        return len(rows)

    def get_asset(self, asset_id: str) -> tuple[Path, str]:
        r = self.conn.execute("SELECT filename, mime FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not r:
            raise BoardError(f"no asset {asset_id}", 404)
        return self.assets_dir / r[0], r[1]


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


def strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


def _not_a_db(e: Exception) -> bool:
    m = str(e).lower()
    return "not a database" in m or "malformed" in m or "lacks columns" in m or "no such table" in m


def _collapse_cr(s: str) -> str:
    """Normalize \\r\\n to \\n and collapse progress-bar style \\r rewrites to the last segment."""
    s = s.replace("\r\n", "\n")
    return "\n".join(line.rsplit("\r", 1)[-1] if "\r" in line else line for line in s.split("\n"))
