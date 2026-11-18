"""Durable storage shared by the CLI (direct writes) and the server (reads + HTTP writes).

SQLite in WAL mode plus an assets/ directory under the data dir. Every mutation writes
its row changes AND an `events` row in one IMMEDIATE transaction, so a committed event
always describes committed state. `events.id` is the global, monotonically increasing
`seq`; each post records the seq of the last event that touched it.

Safe for many concurrent writer processes (busy_timeout + short transactions).
"""

from __future__ import annotations

import csv
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
  seq        INTEGER NOT NULL DEFAULT 0
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
"""


class BoardError(Exception):
    """Validation / lookup failure. `status` is the HTTP status the server should use."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def now_iso() -> str:
    d = datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def new_id(prefix: str, n: int) -> str:
    return prefix + "".join(secrets.choice(_B32) for _ in range(n))


def default_data_dir() -> Path:
    return Path(os.environ.get("BOARD_DIR") or os.environ.get("BOARD_DATA") or Path.home() / ".board").expanduser()


# ---------------------------------------------------------------- validation helpers


def normalize_agent(agent: str | None) -> str:
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
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
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
        if s[0] in "[{":
            try:
                value = json.loads(s)
            except json.JSONDecodeError as e:
                raise BoardError(f"table: invalid JSON at line {e.lineno}, col {e.colno}: {e.msg}")
        else:
            try:
                dialect = csv.Sniffer().sniff(s[:4096], delimiters=",\t;")
            except csv.Error:
                dialect = csv.excel
            rows = [r for r in csv.reader(io.StringIO(s), dialect) if r]
            value = rows
    if isinstance(value, dict) and "columns" in value and "rows" in value:
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
    # log
    return {"exit_code": payload.get("exit_code"), "truncated": False}


# ---------------------------------------------------------------- store


class Store:
    def __init__(self, data_dir: str | Path | None = None):
        self.dir = Path(data_dir).expanduser() if data_dir else default_data_dir()
        self.assets_dir = self.dir / "assets"
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "board.db"
        self.conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._retry(lambda: self.conn.executescript(SCHEMA))

    # -- plumbing

    def close(self) -> None:
        self.conn.close()

    def _retry(self, fn, tries: int = 50):
        for i in range(tries):
            try:
                return fn()
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) and "busy" not in str(e) or i == tries - 1:
                    raise
                time.sleep(0.02 * (i + 1))

    def _tx(self, fn):
        def run():
            c = self.conn
            c.execute("BEGIN IMMEDIATE")
            try:
                out = fn(c)
                c.execute("COMMIT")
                return out
            except BaseException:
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
        """Newest-updated first. `agent` is a prefix match on path segments (lead matches lead/*).
        Returns (posts, seq) read in one snapshot so seq is consistent with the posts."""
        limit = max(1, min(int(limit), 2000))
        c = self.conn
        c.execute("BEGIN")
        try:
            seq = self.current_seq()
            if agent:
                a = agent.strip().lower()
                rows = c.execute(
                    "SELECT * FROM posts WHERE agent=? OR agent LIKE ? ESCAPE '\\' ORDER BY updated_at DESC, seq DESC LIMIT ?",
                    (a, a.replace("_", "\\_").replace("%", "\\%") + "/%", limit),
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM posts ORDER BY updated_at DESC, seq DESC LIMIT ?", (limit,)).fetchall()
            posts = [self._row_to_post(c, r, with_payload) for r in rows]
        finally:
            c.execute("COMMIT")
        return posts, seq

    def events_after(self, seq: int, limit: int = 1000) -> list[dict]:
        """[{id, type, post_id, data}] with id > seq, ascending. `data` is the decoded event payload."""
        rows = self.conn.execute(
            "SELECT id, type, post_id, data FROM events WHERE id>? ORDER BY id LIMIT ?", (seq, limit)
        ).fetchall()
        return [{"id": r[0], "type": r[1], "post_id": r[2], "data": json.loads(r[3])} for r in rows]

    def min_event_id(self) -> int:
        return self.conn.execute("SELECT MIN(id) FROM events").fetchone()[0] or 0

    # -- writing

    def _event(self, c: sqlite3.Connection, etype: str, post_id: str, data: dict) -> int:
        cur = c.execute(
            "INSERT INTO events(type, post_id, data, created_at) VALUES (?,?,?,?)",
            (etype, post_id, "{}", now_iso()),
        )
        seq = cur.lastrowid
        data["seq"] = seq
        c.execute("UPDATE events SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), seq))
        c.execute("UPDATE posts SET seq=? WHERE id=?", (seq, post_id))
        return seq

    def create_post(
        self,
        agent: str,
        kind: str,
        payload: dict,
        title: str | None = None,
        status: str | None = None,
        key: str | None = None,
        live: bool = False,
    ) -> tuple[dict, bool]:
        """Create a post, or with `key` replace this agent's existing post with that key
        (same id and created_at; kind/title/status/payload replaced; log chunks cleared).
        Returns (post, created)."""
        agent = normalize_agent(agent)
        status = normalize_status(status)
        payload = normalize_payload(kind, payload)
        title = (title or "").strip()[:200] or None
        key = (key or "").strip()[:100] or None
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
                    "UPDATE posts SET kind=?, title=?, status=?, payload=?, live=?, updated_at=? WHERE id=?",
                    (kind, title, status, json.dumps(payload, ensure_ascii=False), int(live), ts, pid),
                )
                c.execute("DELETE FROM log_chunks WHERE post_id=?", (pid,))
            else:
                pid, created = new_id("p_", 6), True
                c.execute(
                    "INSERT INTO posts(id, agent, kind, title, status, key, payload, live, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (pid, agent, kind, title, status, key, json.dumps(payload, ensure_ascii=False), int(live), ts, ts),
                )
            post = self.get_post(pid, c)
            post["seq"] = self._event(c, "post", pid, dict(post))
            return post, created

        return self._tx(tx)

    def append(
        self,
        post_id: str,
        text: str | None = None,
        status: str | None = None,
        title: str | None = None,
        done: bool = False,
        exit_code: int | None = None,
        newline: bool = False,
    ) -> dict:
        """Append text to a log/text/markdown post and/or change status/title/live.

        log: text added verbatim (newline=True adds a trailing \\n if missing — one-off appends).
        text: joined with \\n. markdown: joined with \\n\\n.
        exit_code set → payload.exit_code, status ok/error unless status given; done → live false.
        Emits an 'append' event when text was added, else a 'post' event (full post).
        Returns a light result {id, seq, status, title, live, updated_at, size}."""
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
            payload = json.loads(r["payload"])
            new_status = status or r["status"]
            if exit_code is not None:
                payload["exit_code"] = exit_code
                if status is None:
                    new_status = "ok" if exit_code == 0 else "error"
            new_title = (title.strip()[:200] or None) if title is not None else r["title"]
            live = 0 if done else r["live"]
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
                        payload["truncated"] = True
                        for cn, ln in c.execute(
                            "SELECT n, LENGTH(text) FROM log_chunks WHERE post_id=? ORDER BY n", (post_id,)
                        ).fetchall():
                            if size <= LOG_CAP or cn == n:
                                break
                            c.execute("DELETE FROM log_chunks WHERE post_id=? AND n=?", (post_id, cn))
                            size -= ln
                else:
                    field = "text" if kind == "text" else "markdown"
                    sep = "\n" if kind == "text" else "\n\n"
                    chunk = sep + text
                    payload[field] = payload[field] + chunk
            c.execute(
                "UPDATE posts SET payload=?, status=?, title=?, live=?, updated_at=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), new_status, new_title, live, ts, post_id),
            )
            size = None
            if kind == "log":
                size = c.execute("SELECT COALESCE(SUM(LENGTH(text)),0) FROM log_chunks WHERE post_id=?", (post_id,)).fetchone()[0]
            if chunk is not None:
                ev = {
                    "id": post_id,
                    "agent": r["agent"],
                    "kind": kind,
                    "text": chunk,
                    "status": new_status,
                    "title": new_title,
                    "live": bool(live),
                    "truncated": bool(payload.get("truncated")),
                    "exit_code": payload.get("exit_code"),
                    "updated_at": ts,
                }
                seq = self._event(c, "append", post_id, ev)
            else:
                post = self.get_post(post_id, c)
                seq = self._event(c, "post", post_id, dict(post))
            return {"id": post_id, "seq": seq, "status": new_status, "title": new_title, "live": bool(live),
                    "updated_at": ts, "size": size}

        return self._tx(tx)

    # -- assets

    def add_asset(self, data: bytes) -> dict:
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
        self._tx(
            lambda c: c.execute(
                "INSERT INTO assets(id, filename, mime, size, created_at) VALUES (?,?,?,?,?)",
                (aid, filename, mime, len(data), now_iso()),
            )
        )
        return {"id": aid, "mime": mime, "size": len(data), "url": f"/api/assets/{aid}"}

    def get_asset(self, asset_id: str) -> tuple[Path, str]:
        r = self.conn.execute("SELECT filename, mime FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not r:
            raise BoardError(f"no asset {asset_id}", 404)
        return self.assets_dir / r[0], r[1]


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


def strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


def _collapse_cr(s: str) -> str:
    """Normalize \\r\\n to \\n and collapse progress-bar style \\r rewrites to the last segment."""
    s = s.replace("\r\n", "\n")
    return "\n".join(line.rsplit("\r", 1)[-1] if "\r" in line else line for line in s.split("\n"))
