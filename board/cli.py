"""The agent-facing `board` CLI. Spec: docs/contract.md section 2; user text: board/help.py.

Writes go straight to SQLite through `Store` (LocalBackend) unless BOARD_URL is set, in
which case the same calls go over HTTP (HttpBackend). httpx / fastapi / uvicorn are
imported lazily so the common path (`board post`) starts fast.
"""

from __future__ import annotations

import codecs
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Annotated, Any, List, Optional

import typer

from board.help import HELP
from board.store import BoardError, Store, default_data_dir, normalize_agent, normalize_status, normalize_table, strip_ansi

FLUSH_INTERVAL = 0.25
FLUSH_BYTES = 256 * 1024
MAX_PENDING = 10 * 1024 * 1024
HINT = "Run 'board help' for usage."


# ---------------------------------------------------------------- errors


class InputError(Exception):
    """Bad command or input: exit 2."""


class Unreachable(Exception):
    """BOARD_URL set but the server is not reachable: exit 3."""


def warn(msg: str) -> None:
    try:
        sys.stderr.write(f"board: {msg}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------- backends


class LocalBackend:
    def __init__(self, data_dir: Path | None = None):
        self.store = Store(data_dir or default_data_dir())

    def create_post(self, **kw) -> tuple[dict, bool]:
        return self.store.create_post(**kw)

    def append(self, post_id: str, **kw) -> dict:
        return self.store.append(post_id, **kw)

    def add_asset(self, data: bytes) -> dict:
        return self.store.add_asset(data)

    def get_post(self, post_id: str) -> dict:
        return self.store.get_post(post_id)

    def list_posts(self, agent: str | None, limit: int) -> list[dict]:
        posts, _ = self.store.list_posts(agent, limit, with_payload=False)
        # ls falls back to the text for untitled posts; fetch payloads only for those.
        for i, p in enumerate(posts):
            if not p["title"] and p["kind"] in ("text", "markdown", "log"):
                posts[i] = self.store.get_post(p["id"])
        return posts


class HttpBackend:
    def __init__(self, url: str):
        import httpx  # lazy: only when BOARD_URL is set

        self.httpx = httpx
        self.url = url.rstrip("/")
        self.client = httpx.Client(base_url=self.url, timeout=httpx.Timeout(15.0, connect=5.0))

    def _req(self, method: str, path: str, **kw) -> Any:
        try:
            r = self.client.request(method, path, **kw)
        except self.httpx.TransportError as e:
            raise Unreachable(f"cannot reach BOARD_URL {self.url}: {e or type(e).__name__}") from e
        if r.status_code >= 400:
            try:
                msg = r.json().get("error") or r.text
            except ValueError:
                msg = r.text.strip() or r.reason_phrase
            raise BoardError(f"{msg} (HTTP {r.status_code})", r.status_code)
        try:
            return r.json()
        except ValueError as e:
            raise BoardError(f"BOARD_URL {self.url} did not answer like a board server", 502) from e

    def create_post(self, **kw) -> tuple[dict, bool]:
        body = {k: v for k, v in kw.items() if v is not None}
        post = self._req("POST", "/api/posts", json=body)
        created = post.pop("created", True)
        return post, created

    def append(self, post_id: str, **kw) -> dict:
        body = {k: v for k, v in kw.items() if v is not None and v is not False}
        return self._req("POST", f"/api/posts/{post_id}/append", json=body)

    def add_asset(self, data: bytes) -> dict:
        return self._req("POST", "/api/assets", files={"file": ("image", data)})

    def get_post(self, post_id: str) -> dict:
        return self._req("GET", f"/api/posts/{post_id}")

    def list_posts(self, agent: str | None, limit: int) -> list[dict]:
        params: dict = {"limit": limit}
        if agent:
            params["agent"] = agent
        return self._req("GET", "/api/posts", params=params)["posts"]


def backend():
    url = os.environ.get("BOARD_URL", "").strip()
    return HttpBackend(url) if url else LocalBackend()


# ---------------------------------------------------------------- input helpers


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


def read_file_bytes(path: str) -> bytes:
    p = Path(path).expanduser()
    if not p.is_file():
        raise InputError(f"file not found: {path}")
    try:
        return p.read_bytes()
    except OSError as e:
        raise InputError(f"cannot read {path}: {e.strerror}") from e


def read_value(v: str) -> str:
    """Value rule: literal, `-` = all of stdin, `@path` = file contents."""
    if v == "-":
        return read_stdin_bytes().decode("utf-8", "replace")
    if v.startswith("@") and len(v) > 1:
        return read_file_bytes(v[1:]).decode("utf-8", "replace")
    return v


def agent_name(agent: str | None) -> str:
    try:
        return normalize_agent(agent or os.environ.get("BOARD_AGENT"))
    except BoardError as e:
        raise InputError(e.message) from e


def status_name(status: str | None) -> str | None:
    if status is None:
        return None
    try:
        return normalize_status(status)
    except BoardError as e:
        raise InputError(e.message) from e


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def out(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def emit(obj: dict, as_json: bool, quiet: bool) -> None:
    if quiet:
        return
    out(dumps(obj) if as_json else obj["id"])


# ---------------------------------------------------------------- streaming engine


class Streamer:
    """Echo bytes to stderr and ship them to a post in batches (every 250 ms or 256 KB).

    Storage/HTTP failures are retried with the text kept in memory (capped at 10 MB); they
    never stop the echo or the producer. A rejected post (BoardError: deleted, wrong kind)
    stops shipping but keeps echoing.
    """

    def __init__(self, be, post_id: str | None, echo: bool):
        self.be = be
        self.post_id = post_id
        self.echo = echo
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.pending = ""
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.dead = post_id is None
        self.warned = False
        self.dropped = False
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
        """Take what is ready to send, holding back a trailing partial ANSI escape or a
        lone trailing \\r (it may be the first half of \\r\\n) until more data arrives."""
        with self.lock:
            s = self.pending
            if final:
                s += self.decoder.decode(b"", final=True)
                self.pending = ""
            else:
                cut = len(s)
                esc = s.rfind("\x1b")
                if esc != -1 and len(s) - esc < 64 and strip_ansi(s[esc:]).startswith("\x1b"):
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

    def _send(self, text: str, **kw) -> bool:
        """One append; returns True on success. Transient failures give the text back."""
        try:
            self.be.append(self.post_id, text=text or None, **kw)
            self.warned = False
            return True
        except BoardError as e:
            self.dead = True
            warn(f"stopped posting output to {self.post_id}: {e.message}")
            return True
        except Exception as e:  # sqlite busy, HTTP down, ...: keep the text and retry
            if text:
                self._giveback(text)
            if not self.warned:
                warn(f"could not post output to the board (will retry): {e}")
                self.warned = True
            return False

    def _run(self) -> None:
        backoff = FLUSH_INTERVAL
        while not self.stopping.is_set():
            self.wake.wait(backoff)
            self.wake.clear()
            if self.stopping.is_set() or self.dead:
                break
            text = self._take(final=False)
            if not text:
                backoff = FLUSH_INTERVAL
                continue
            backoff = FLUSH_INTERVAL if self._send(text) else min(backoff * 2, 5.0)

    def finish(self, tries: int = 4, **final_kw) -> None:
        """Stop the flusher and send the rest together with the final status change."""
        self.stopping.set()
        self.wake.set()
        self.thread.join()
        if self.dead:
            return
        text = self._take(final=True)
        if self.dropped:
            text = "[board: output was dropped while the board was unreachable]\n" + text
        if not text and not final_kw:
            return
        for i in range(tries):
            if self._send(text, **final_kw):
                return
            text = self._take(final=True)
            time.sleep(0.25 * (i + 1))
        warn(f"giving up on posting the end of the output to {self.post_id}")


def pump(fd: int, streamer: Streamer) -> None:
    while True:
        try:
            data = os.read(fd, 65536)
        except InterruptedError:
            continue
        except OSError:
            break
        if not data:
            break
        streamer.feed(data)


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
    if image is not None:
        data = read_stdin_bytes() if image == "-" else read_file_bytes(image)
        be = backend()
        asset = be.add_asset(data)
        kind, payload = "image", {"asset_id": asset["id"]}
    else:
        if chart is not None:
            raw = read_value(chart)
            try:
                opt = json.loads(raw)
            except json.JSONDecodeError as e:
                raise InputError(f"chart: invalid JSON at line {e.lineno}, col {e.colno}: {e.msg}") from e
            if not isinstance(opt, dict):
                raise InputError('chart must be a JSON object (an ECharts option with "series")')
            kind, payload = "chart", {"option": opt}
        elif table is not None:
            try:
                kind, payload = "table", normalize_table(read_value(table))
            except BoardError as e:
                raise InputError(e.message) from e
        elif md is not None:
            kind, payload = "markdown", {"markdown": read_value(md)}
        else:
            kind, payload = "text", {"text": read_value(sources[0])}
        be = backend()
    post, created = be.create_post(agent=a, kind=kind, payload=payload, title=title, status=s, key=key)
    post["created"] = created
    emit(post, as_json, quiet)


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
    if not cmd and stdin_is_tty():
        raise InputError("pipe output into board log, or use: board log -a NAME -- CMD")
    if cmd and not title:
        title = shlex.join(cmd)
    be = backend()
    post_id = None
    create_error: Exception | None = None
    try:
        post, created = be.create_post(agent=a, kind="log", payload={}, title=title, status=s, key=key, live=True)
        post["created"] = created
        post_id = post["id"]
        emit(post, as_json, quiet)
    except (BoardError, Unreachable) as e:
        # Never break the producer: still run / drain it and echo, just don't post.
        create_error = e
        warn(f"{getattr(e, 'message', e)}; {'running the command' if cmd else 'passing output through'} without posting")

    streamer = Streamer(be, post_id, echo=not quiet)
    streamer.start()

    if not cmd:  # pipe mode
        pump(sys.stdin.fileno(), streamer)
        streamer.finish(done=True)
        if create_error is not None:
            raise create_error
        return

    env = dict(os.environ, PYTHONUNBUFFERED="1")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    except OSError as e:
        rc = 127 if isinstance(e, FileNotFoundError) else 126
        msg = f"cannot run {cmd[0]}: {e.strerror or e}"
        warn(msg)
        if post_id:
            streamer._giveback(f"board: {msg}\n")
        streamer.finish(done=True, exit_code=rc)
        raise typer.Exit(rc)

    def forward(signum, _frame):
        try:
            proc.send_signal(signum)
        except OSError:
            pass

    old = {sig: signal.signal(sig, forward) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        pump(proc.stdout.fileno(), streamer)
        while True:
            try:
                rc = proc.wait()
                break
            except InterruptedError:
                continue
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)
    if rc < 0:
        rc = 128 - rc
    streamer.finish(done=True, exit_code=rc)
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
    s = status_name(status)
    if text is None and s is None and title is None:
        raise InputError("give TEXT to add, or -s STATUS / -t TITLE to change")
    if text == "-":
        if stdin_is_tty():
            raise InputError("nothing on stdin")
        be = backend()
        post = be.get_post(post_id)
        if post["kind"] not in ("log", "text", "markdown"):
            raise BoardError("can only append to log, text or markdown posts; use --key to replace", 409)
        streamer = Streamer(be, post_id, echo=not quiet)
        streamer.start()
        pump(sys.stdin.fileno(), streamer)
        final = {k: v for k, v in (("status", s), ("title", title)) if v is not None}
        streamer.finish(**final)
        if streamer.dead:
            raise typer.Exit(1)
        res = be.get_post(post_id)
        res.pop("payload", None)
    else:
        value = read_value(text) if text is not None else None
        res = backend().append(post_id, text=value, status=s, title=title, newline=value is not None)
    emit(res, as_json, quiet)


@app.command("ls")
def ls_cmd(
    agent: AgentOpt = None,
    limit: Annotated[int, typer.Option("-n", "--limit")] = 20,
    as_json: JsonOpt = False,
    quiet: QuietOpt = False,
) -> None:
    posts = backend().list_posts(agent, limit)
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
        out("\t".join([p["id"], p["agent"], p["kind"], p["status"], label]))


@app.command("serve")
def serve_cmd(
    port: Annotated[int, typer.Option("--port", "-p")] = 8000,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    data: Annotated[Optional[Path], typer.Option("--data")] = None,
) -> None:
    import httpx

    data_dir = (data or default_data_dir()).expanduser()
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{host}:{port}"
    try:
        r = httpx.get(f"http://{probe_host}:{port}/api/posts", params={"limit": 1}, timeout=1.0)
        if r.status_code == 200 and "posts" in r.json():
            warn(f"already running at {url}")
            out(url)
            return
    except Exception:
        pass

    import uvicorn

    from board.server import create_app

    application = create_app(data_dir)
    warn(f"serving {url}  data: {data_dir}")
    if host in ("0.0.0.0", "::"):
        warn(f"listening on {host} with no auth: anyone who can reach this port can read and post. "
             "Prefer an SSH tunnel (ssh -L 8000:localhost:8000 host).")
    out(url)
    uvicorn.run(application, host=host, port=port, workers=1, timeout_graceful_shutdown=2, log_level="warning")


# ---------------------------------------------------------------- entry point


def wants_help(args: list[str]) -> bool:
    if not args or args[0] == "help":
        return True
    before = args[: args.index("--")] if "--" in args else args
    return "-h" in before or "--help" in before


def run(args: list[str]) -> int:
    if wants_help(args):
        out(HELP.rstrip("\n"))
        return 0
    try:
        command = typer.main.get_command(app)
        rv = command.main(args, prog_name="board", standalone_mode=False)
        return rv if isinstance(rv, int) else 0
    except InputError as e:
        warn(str(e))
        warn_hint()
        return 2
    except BoardError as e:
        warn(e.message)
        return 1
    except Unreachable as e:
        warn(str(e))
        return 3
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
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
    except (OSError, ValueError):
        pass


def main() -> None:
    code = run(sys.argv[1:])
    try:
        sys.stdout.flush()
    except (BrokenPipeError, OSError, ValueError):
        # Don't let a closed stdout turn into a traceback at interpreter shutdown.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
    sys.exit(code)


if __name__ == "__main__":
    main()
