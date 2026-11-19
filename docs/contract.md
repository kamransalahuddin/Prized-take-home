# board — frozen contract (CLI ⇄ store ⇄ server ⇄ page)

Read this whole file before building any part. If something here is wrong or missing,
report it instead of changing it silently. Source of truth for storage: `board/store.py`
(already implemented; reuse it, don't reimplement). Source of truth for CLI behavior
and `board help`: `board/help.py` + section 2.

## 1. Architecture

```
agents ──board CLI──► store.py ──► SQLite WAL (~/.board/board.db) + ~/.board/assets/
                      (direct writes, no server needed)        ▲
remote agents ──HTTP POST /api/*──► server.py ──store.py───────┘
                                        │ watcher: every 100ms check store.data_version();
                                        │ on change read store.events_after(last_seq)
                                        ▼
                                    SSE /api/events ──► browser (vanilla JS)
```

- Data dir: `--data DIR` > `BOARD_DIR` > `~/.board`. `Store(data_dir)` creates it.
- CLI writes via `Store` directly **unless `BOARD_URL` is set**, then it uses HTTPX against
  the HTTP API below (same semantics; exit 3 if unreachable after retries, see "Idempotent
  writes" below).
- One Uvicorn worker. In-process asyncio fan-out (one `asyncio.Queue` per SSE client).
- Server's own HTTP writes go through the same `Store`; the watcher picks them up like
  any other write (single code path for broadcasting). Persist-before-broadcast holds
  because events are only read after commit.

## 2. CLI (Typer, `board = board.cli:main`)

See `board/help.py` for the user-facing text (verbatim; must stay ≤80 cols, ≤120 lines).

- Typer app: `add_completion=False, rich_markup_mode=None, pretty_exceptions_enable=False`,
  disable built-in help; `-h/--help` on root and every subcommand, `board help`, and bare
  `board` all print `HELP` and exit 0. Usage errors: stderr `board: <msg>` + `Run 'board help' for usage.`, exit 2.
- **Value rule** for TEXT / `--md` / `--table` / `--chart` and `append TEXT`: literal text;
  `-` = read all stdin (error exit 2 `nothing on stdin` if TTY or empty); `@path` = read
  the file if it exists. `@text` naming no file is literal text (`@alice.smith`, `@v1.2`),
  unless it looks like a path (contains `/`, starts with `~`, or ends in a file extension
  such as .md .txt .csv .json .svg .png): then exit 2 `file not found: path (to post text
  starting with @, pipe it in with -)`. `--image FILE` takes a path or `-` (bytes).
- Command-line strings with invalid UTF-8 are repaired (bad bytes → U+FFFD), never a
  traceback. `-h`/`--help` means help only as a standalone token, not as an option value
  (`board post -a x -t -h text` posts with title `-h`).
- Keys: ≤ 100 chars, not blank (else exit 2 / HTTP 400; never truncated). Titles longer
  than 200 chars are cut (the CLI warns). JSON input (chart/table/API) rejects NaN/Infinity,
  numbers that overflow to Infinity (`1e999`) and nesting deeper than 64 levels (exit 2 /
  400; checked before sending in `BOARD_URL` mode too). Payloads are capped at 8 MB
  serialized in direct mode too (exit 1, "larger than 8 MB"); tables > 50,000 rows exit 1.
  Also exit 2 / 400: empty or whitespace-only text/Markdown, a table without a header and
  at least one data row (JSON or CSV), chart `series` that is not a list or object,
  `live` on a non-log post, a non-boolean `payload.ask`, a log `payload.pid` outside
  1..2^31-1. Agent names are ≤ 64 chars (the error says so).
- Hidden aliases: `--text` (= positional TEXT), `--markdown` (= `--md`).
- `post`: exactly one content source, else exit 2 `use exactly one of TEXT, --md, --table, --chart, --image`.
  `-a/--agent` (fallback `BOARD_AGENT`), `-t/--title`, `-s/--status`, `-k/--key`, `--json`, `-q`.
  table → `store.normalize_table(value)`; chart → must parse as JSON object (exit 2 with
  line/col on bad JSON); image → `store.add_asset(bytes)` then post `{"asset_id": id}`.
- `log`: `board log -a NAME [-t T] [-s S] [-k K] [--json] [-q] [-- CMD ARGS...]`
  - Creates the `kind="log", live=True` post (payload `pid`, `host`, `pstart` = process
    start time, for the reaper) and prints its ID (flush). The create runs in the flusher
    thread with ONE fixed `request_id`, retried on transient failures while output is
    buffered, so a create that commits late is the post that receives the output. The
    command starts as soon as the post exists, or after 3 s regardless (warning; the ID is
    printed when the post is created). If it is never created, warn once with the number
    of bytes not posted. The backend (the store, which can wait 30 s on a lock) is opened
    in the flusher thread too, so it never delays the command start or the stdin drain
    (`append -` likewise reads the target post there).
  - Run mode: spawn CMD (no shell) **in its own process group** (`start_new_session`) with
    stdin `/dev/null` (no terminal either: commands that prompt need `-m`, `--yes`, …),
    stderr merged into stdout, env `PYTHONUNBUFFERED=1` plus `GIT_EDITOR=true`,
    `GIT_SEQUENCE_EDITOR=true` and `GIT_TERMINAL_PROMPT=0`, always (overriding exported
    values: git fails fast instead of opening an editor or password prompt nobody can
    answer; `EDITOR`/`VISUAL` are passed through unchanged, so other tools that start
    `$EDITOR` still need a non-interactive flag), default title = command line. SIGINT/SIGTERM/SIGHUP/SIGQUIT handlers are installed before the spawn;
    each received signal is forwarded once to the child's whole group (one arriving before
    the spawn: the command is not started). If the child is still running 5 s after a
    forwarded signal, the group gets SIGKILL and board exits 128+N. board finishes when
    the child itself exits (pipe drained for ≤ 1 s more; background grandchildren holding
    the pipe are left alone): `append(id, done=True, exit_code=rc)`; board exits with rc
    (128+N if the child died from signal N). After a forwarded stop signal, once the
    child exits, the rest of its process group gets SIGTERM and, 1 s later, SIGKILL (so
    `sh -c 'sleep 100 & wait'` + Ctrl-C leaves no orphans); without a stop signal,
    backgrounded grandchildren are left alone. Processes that left the group survive
    INT/TERM like `setsid` daemons do: e.g. background jobs of a shell with job control
    (`sh -c 'set -m; sleep 100 & wait'`), which get their own process groups. A signal during the final retry stops
    retrying (warning with the bytes not posted); the exit code is still the child's.
    Handlers stay installed until board exits, so a storm of signals always exits 128+N
    of the first one (never killed by a late one): they only record the signal and wake
    a watcher thread (self-pipe), which forwards it and stops retries, so nested handlers
    cannot deadlock or recurse; at the very end board ignores the stop signals, flushes
    stdout/stderr and leaves with `os._exit`. `kill -9` of `board log` itself leaves
    its child running (macOS has no parent-death signal); the post is closed by the reaper.
  - Pipe mode (no CMD): read stdin (exit 2 if TTY: `pipe output into board log, or use: board log -a NAME -- CMD`). EOF → `append(id, done=True)`.
    SIGINT/SIGTERM/SIGHUP/SIGQUIT: drain stdin ≤ 1 s, post what was read (≤ 10 s), close
    the post (`done`, status `error`), exit 128+N. If posting stopped for good (warned, e.g.
    disk full) board still drains stdin, then exits 1, like `append -`.
  - Reader thread copies every byte to stderr (unless `-q`) and buffers; flusher sends
    `append(id, text, expect_gen=GEN)` every 250 ms or at 256 KB (≤ 256 K chars per chunk).
    Strip ANSI (`store.strip_ansi`). Store/HTTP failure never kills the child or stops the
    echo (buffer ≤10 MB): transient failures (HTTP connect/timeout/5xx, a locked DB) are
    retried; permanent ones (4xx, post replaced → 409, disk full, read-only, not a board DB,
    data dir unusable) stop posting with one warning naming the real cause. Even if the
    store cannot be opened at all, the command runs (or stdin is drained) and is echoed.
  - A second `board log -k K` replaces the post and its `gen`; the first writer's appends
    then get 409 and it stops posting (its child keeps running, exit code passed through).
- `append ID [TEXT] [-s S] [-t T] [--json] [-q]`: TEXT literal / `-` / `@path`;
  one-off literal → `store.append(id, text, newline=True)`; `-` streams like log pipe mode
  (with the post's `gen` as `expect_gen`; signals flush and exit 128+N; a log post is also
  closed with status `error`). Into a text/Markdown post only the first streamed chunk is
  joined with the separator; later chunks are sent with `raw: true` and continue it
  verbatim, wherever the 250 ms flushes fall. No TEXT and no -s/-t → exit 2. A malformed ID → exit 2.
- `ask -a NAME [-t T] [-k K] [--timeout SECONDS] [--json] [-q] QUESTION` (QUESTION: value rule):
  creates a `text` post, status `blocked`, `payload.ask = true`; stderr
  `board: waiting for the human on ID (board wait ID to resume)`; then waits like `wait`.
- `wait ID [--timeout SECONDS] [--json] [-q]`: waits until the post has `payload.reply`, then
  stdout line 1 = `approve` | `reject` | `answer`, then the reply text (if any) on the next
  lines (`--json`: one line `{"id","action","text","at"}`); exit 0. Already replied →
  prints at once. Not blocked and no reply → exit 1 `post is not waiting for input`
  (also if it stops being blocked without a reply while waiting). Polls the store every
  0.5 s, or with `BOARD_URL` `GET /api/posts/{id}` every 1 s (each read times out after
  ≤ 5 s, or what is left of `--timeout`), warning once and waiting on
  while the server is down (connection errors, timeouts, a locked DB; 5xx for ≤ 30 s).
  Anything else (unknown post, bad ID → exit 2, corrupt DB) exits at once. The waiter
  remembers the question's `gen`: if the question is replaced (re-asked with `-k`), it
  exits 1 `your question was replaced by a newer one (board wait ID)` instead of printing
  an answer to a different question. `--timeout` (default: forever) → exit 4, the question
  stays open. Ctrl-C → exit 130. `-q`: print nothing.
- `ls [-a NAME] [-n 20] [--json]`: `store.list_posts(agent, limit, with_payload=False)`;
  TSV lines `ID\tAGENT\tKIND\tSTATUS\tTITLE` (title fallback: first 60 chars of text, else `-`).
  `-n` < 1 → exit 2; `-a /` → exit 2 `bad agent name`. A database this user cannot write
  is opened read-only (`ls` works; writes still exit 1). Over HTTP `ls` gives up after 5 s
  (refused connections retried; a server that accepts but never answers: the read timeout
  is capped at what is left of the 5 s), exit 3.
- `serve [--port 8000] [--host 127.0.0.1] [--data DIR]`: if port already serves a board
  (`GET /api/posts?limit=1` ok) print `board: already running at URL`, exit 0. Print
  `board: serving http://HOST:PORT  data: DIR` to stderr, URL to stdout. Warn on stderr
  when host is `0.0.0.0` (no auth; use an SSH tunnel). Runs uvicorn with
  `timeout_graceful_shutdown=2`, one worker. SIGINT/SIGTERM/SIGHUP/SIGQUIT all shut down
  cleanly (WAL checkpoint), exit 0. A closed stdout/stderr is reopened on `/dev/null`.
  Bind errors name the cause: unknown host name, address not on this machine, port in use,
  permission denied.
- stdout: post ID + `\n` (or one-line JSON with `--json`). Everything else → stderr.
- Exit codes: 0 ok · 1 rejected (`BoardError`, HTTP 4xx, 5xx after retries, database
  locked > 30 s, disk full / read-only / not a board database) · 2 usage/input (incl. a
  data path that is not a directory or too long) · 3 `BOARD_URL` unreachable after retries
  (no connection; ~20 s for a write, ~5 s for `ls`) · 1 also when a write reached the
  server but no answer came back within the retries (`the write may already have been
  applied (request id …); check with board ls`)
  · 4 `ask`/`wait` timed out with no reply · `log -- CMD` → CMD's code. Errors are one
  `board: …` line on stderr, never a traceback. A closed stdout never fails a command.
- Idempotent writes: the CLI sends a fresh uuid4 `request_id` with every logical write
  (post, append, each streamed log chunk, reply, asset) and reuses it on every retry of
  that write. A chunk being retried keeps its exact text; newer output goes in a later
  chunk. Over HTTP, failures are **transient** (connection error, timeout, HTTP 5xx, 408,
  429: the write may or may not have happened, so retry with the same id) or
  **permanent** (other 4xx: never retried). One-shot writes retry transient failures with
  backoff for `BOARD_WRITE_PATIENCE` seconds (default 20) and at least 4 attempts
  (connect timeout 3 s, read timeout 30 s: a slow answer is not a failure; a timed-out
  request may have committed; if every attempt took longer, give up after 3× the
  patience), then exit 3 (never connected) or 1 (5xx, or a request that was sent but
  never answered: "may already have been applied (request id …)"). Streams retry for as long as the command runs, then
  `BOARD_FINISH_PATIENCE` (default 60) s for the final chunk. A locked local database is
  retried for `BOARD_LOCK_PATIENCE` (default 30) s in total, then exit 1 `database is
  locked by another process`. An invalid patience value (not a number, negative, NaN,
  inf) is ignored with one warning and the default is used.
- Direct mode closes orphaned live logs (see `reap_dead_logs`) at most once a minute from
  `board post` / `board ls` (marker file `BOARD_DIR/.reaped`).

## 3. Data model

Post object (from `store.get_post` / `list_posts`, in API responses and `post` events):

```json
{"id":"p_3fak2a7q","agent":"lead/tests","kind":"text|markdown|table|chart|image|log",
 "title":"pytest or null","status":"info|ok|warn|error|blocked","key":"latency or null",
 "live":false,"created_at":"2026-11-18T16:45:12.345Z","updated_at":"...","seq":1042,
 "gen":"9f2c4e1a7b3d5c60","payload":{...}}
```

- `id`: `p_` + 8 base32 chars for new posts (older posts have 6; accept `p_[a-z2-7]{4,16}`).
- `gen`: random token, new on every create **and every `--key` replace** (not on appends,
  status changes or replies, except re-blocking an answered post, below). Writers that
  must not touch a replacement pass it back as `expect_gen`.

| kind | payload |
|---|---|
| text | `{"text": "..."}` |
| markdown | `{"markdown": "..."}` |
| table | `{"columns": ["a","b"], "rows": [["1", 2], ...]}` cells: str/number/bool/null |
| chart | `{"option": {ECharts option}}` |
| image | `{"asset_id":"a_9c1d7e2f","url":"/api/assets/a_9c1d7e2f","mime":"image/png"}` |
| log | `{"text":"...","truncated":false,"exit_code":null}` (last 2,097,152 code points kept) |

`seq` = id of the last event that touched the post. Global, monotonic. Lists and the
snapshot are ordered by `seq` descending (most recently changed first; not by clock).

Human replies (any non-log kind, normally `text`):
- `payload.ask = true` — made by `board ask` (the page shows the reply buttons;
  a post made with `-s blocked` without it can be answered too).
- `payload.reply = {"action": "approve"|"reject"|"answer", "text": str|null,
  "at": "2026-11-18T16:45:12.345Z"}` — set once by `POST /api/posts/{id}/reply`
  (`Store.reply`); the same write sets status `blocked` → `info`. A `--key` replace
  replaces the payload, so it clears the reply (re-asking with the same key works).
  Setting status `blocked` again on an answered post (`board append ID -s blocked`)
  clears `payload.reply` and bumps `gen` (emits a full `post` event).

Idempotency: table `requests(id, result, created_at, fp)`. `create_post`, `append`, `reply`
and `add_asset` take an optional `request_id`. `fp` = sha256 of the operation plus its
normalized arguments (target post id, agent, kind, payload, title, status, key, live /
text, status, title, done, exit_code, newline, expect_kind, expect_gen / action, text,
expect_gen / image bytes). In the same transaction: a known id with the same `fp` returns
the stored result without writing; a known id with a different `fp` (other operation,
post or arguments) → 409 `request_id was already used for a different request`. For
create the post id, `created` and the post's `gen` are stored and the post is re-read on
replay; if its `gen` changed since (replaced with `--key` by a newer request) the replay
is a 409 `post … was replaced (--key) by a newer post since this request`, never the
newer post (so a late retry of a first `board log` / `board ask` cannot adopt the second
one). For reply the post id, `gen` and reply time are stored; if the question was
re-asked / re-opened since, the replay is a 409 (`… was replaced or re-opened since …`).
The newest 100,000 rows are kept (pruned by insertion order, not by clock: a clock jump
cannot forget recent ids). (Rows from older versions without `fp` match on op only; an
old-format stored create gets the current `gen` filled in.)

Storage limits: logs keep exactly the last 2,097,152 (2 × 1024 × 1024) characters (code
points; `truncated: true` once trimmed). The `events` table keeps the last 20,000 events
and at most ~64 MB of event data (`events.size`; the oldest beyond either limit are
pruned); a resume from before that gets `reset`. Stored data too deeply nested to
serialize (written by an older version) is served as `{"error": …}` instead of failing. `meta(key, value)` holds the database `epoch` (random,
created with the schema). Opening a file that is not a board database (or an incompatible
one) fails with `… is not a board database (or is from an incompatible version)`. A
locked database is retried for 30 s in total (`StoreBusy`, HTTP 503).

## 4. HTTP API (server.py; thin wrapper over Store; errors → `{"error": msg}` with `BoardError.status`)

| Method/path | Body → Response |
|---|---|
| `POST /api/posts` | `{"agent","kind","payload","title"?,"status"?,"key"?,"live"?,"request_id"?}` → 201 post+`"created":true` / 200 on key replace (`"created":false`). Body limit 8 MB (413). `live` must be a JSON boolean (400 otherwise); log `payload.exit_code` an integer or null. |
| `POST /api/posts/{id}/append` | `{"text"?,"status"?,"title"?,"done"?,"exit_code"?,"newline"?,"raw"?,"expect_kind"?,"expect_gen"?,"request_id"?}` (`done`/`newline`/`raw` booleans; `raw: true` = add text/Markdown without the separator, for stream continuations) → 200 `{"id","seq","status","title","live","updated_at","size"}`; 404 / 409 (wrong kind, or `expect_gen` ≠ the post's `gen`: `post … was replaced (--key) by a newer post; not appending`) |
| `POST /api/posts/{id}/reply` | `{"action":"approve"\|"reject"\|"answer","text"?,"expect_gen"?,"request_id"?}` (`answer` needs non-empty text; text ≤ 10000 chars) → 200 full post (`payload.reply` set, status `info`, emits `post`); 400 bad action/text · 404 · 409 `{"error":"the question was replaced by a newer one; reload and answer that"}` (`expect_gen` given and ≠ the post's `gen`: the page sends the `gen` of the question it displayed, so an answer never lands on a re-asked question) / `{"error":"already answered"}` / `{"error":"post is not waiting for input"}` (status not `blocked`) |
| `POST /api/assets` | multipart field `file` (+ optional form field `request_id` or header `X-Request-Id`) → 201 `{"id","mime","size","url"}`; 415 / 413 |
| `GET /api/posts?agent=&limit=200` | → `{"posts":[...], "seq": N, "epoch": "e3b0c442"}` (N consistent with posts; newest `seq` first; `limit` ASCII digits only, else 400) |
| `GET /api/posts/{id}` | → post |
| `GET /api/assets/{id}` | bytes, stored MIME, `Cache-Control: public, max-age=31536000, immutable`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox` |
| `GET /api/events` | SSE, see §5 |
| `GET /` | `board/static/index.html`; `/static/*` serves `board/static/` (incl. `vendor/`) |

`request_id` (optional string ≤ 200 chars) on any write makes it idempotent: repeating the
same request returns the first response's body and writes nothing; the same id with a
different request → 409 (see §3).

Errors: 4xx are permanent (fix the request). 5xx are transient and safe to retry with the
same `request_id`: 503 `{"error":"database is locked by another process …"}`, 503
`{"error":"board data directory was removed or replaced and cannot be reopened …"}` (the
server checks `board.db` and its `-wal` still are the files it opened — before every read
and write and every second; if not, it reopens the data dir's new database (new epoch, so
SSE clients are disconnected and get `reset`), and never reads from or reports success for
a write into a deleted database), 503
`board database error: …` / `board storage error: …` (sqlite / OS errors, e.g. disk full).
Invalid JSON, NaN/Infinity, lone surrogates (`"\ud800"`), absurd nesting, wrong types → 400.

## 5. SSE wire format (`GET /api/events`)

Resume point = `Last-Event-ID` header (browser sends it automatically on reconnect) or
`?since=` query (used on first connect: `EPOCH:SEQ` from the `epoch` and `seq` of
`GET /api/posts`). Both accept `EPOCH:SEQ` or plain `SEQ` (= the current epoch; backward
compatible), ASCII digits only (`1_0`, full-width digits, negatives → 400). A different
epoch (database wiped / replaced / another `--data`), a SEQ newer than the current one, or
one older than the oldest kept event → `event: reset`.

```
retry: 1000

event: hello
data: {"seq": 1041, "epoch": "e3b0c442"}

id: e3b0c442:1042
event: post
data: {full post object, incl. "seq" and "gen"}

id: e3b0c442:1043
event: append
data: {"id":"p_3fak2a7q","agent":"lead/tests","kind":"log","text":"chunk\n","status":"info",
       "title":"pytest","live":true,"truncated":false,"exit_code":null,
       "updated_at":"...","seq":1043}

event: ping
data: {"t": 1763484312}     (every 15 s, on a timer, whether or not events are flowing;
                             no id: Last-Event-ID is unaffected. A named event because
                             EventSource hides `:` comments from scripts: a page can
                             reconnect when nothing arrived for 45 s.)
```

`reset` carries `{"seq": N, "epoch": "…"}` (and `id: EPOCH:N`): refetch the snapshot.

- Server: register subscriber queue FIRST, then replay `events_after(since)` in batches,
  then stream live; drop any event with id ≤ last sent (dedupe). If `since` is older than
  `min_event_id()-1` (the events were pruned) send `event: reset`. Events are never skipped
  silently: if pruning overtakes a slow replay (a batch does not start at last+1), the
  stream sends `reset` and goes on from the current end; if the live watcher falls behind
  pruning (server stalled while > 20,000 events / 64 MB were written), every stream is
  ended and reconnects into a `reset`. `reset` carries `id: EPOCH:SEQ` (the position the
  stream continues from), so the browser's Last-Event-ID moves to the current epoch and the
  next reconnect does not reset again; `hello` and `ping` carry no id.
- `post` = create / key replace (new `gen`) / reply / re-blocking an answered post (full
  object). `append` = every other change to an existing post: a text chunk and/or the
  current status/title/live/truncated/exit_code. A status/title/`done`/exit_code change
  without new text is an `append` with `"text": ""` (never a full post: a log can be over 2 MB).
- Log truncation is deterministic: the server keeps exactly the last 2,097,152
  (2 × 1024 × 1024) code points of the concatenated chunks. Client: append each chunk,
  then keep the last 2,097,152 code
  points (and show "truncated" when the event says so).
- Server limits: replay and the watcher read events in batches of ≤ 500/1000 events and
  ~1 MB; each SSE client may have ≤ 5000 frames / 8 MB queued, else it is dropped (queue
  freed at once; it reconnects and replays); every send has a 30 s timeout, so a stalled
  or half-open client is cut off (connection aborted, socket released) and its memory
  freed. On shutdown, streams blocked on such clients end at once.
- Client: on `post` replace the post if `data.seq > local.seq`; on `append` add `text` to
  the local post's text (log: concat; text: already has `\n` prefix; markdown: `\n\n`
  prefix — the chunk is the exact suffix to concatenate) if `data.seq > local.seq`, then
  set status/title/live/seq. On `reset`, refetch snapshot.

## 6. Page (board/static/index.html, app.css, app.js — no build step)

- Libraries (already vendored, load locally): `/static/vendor/echarts.min.js`,
  `markdown-it.min.js`, `purify.min.js`. No CDN, no web fonts download (use
  `Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif` and
  `"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace`).
- Look like prized.dev/dashboard (tokens from its CSS):
  - type: base 15px/24px, sm 13px/18px, xs 12px/16px, lg 17px, xl 20px; radius 8px, panels 12px.
  - light: window `#efeff0` (page bg), card `#fff`, fg `#1b1b1b`, fg-2 `#2f2f31`,
    muted `#5c5c5e`, faint `#9c9d9f`, muted-bg `#ececed`, accent-bg `#e1e1e2`,
    border `#dedede`, border-subtle `#f1f1f1`, primary `#0047d8`, ok `#26a544`,
    warn `#e0b100`, bad `#e33e46`, busy `#0047d8`,
    shadow `0 3px 6px -2px #00000005, 0 1px 1px #0000000a`.
  - dark (`prefers-color-scheme: dark`): window `#09090a`, bg `#121213`, card `#161617`,
    fg `#fff`, fg-2 `#e3e3e6`, muted `#959597`, faint `#565759`, muted-bg `#1a1a1b`,
    accent-bg `#1d1d1f`, border `#232426`, border-subtle `#1a1b1d`, primary `#3b6cff`,
    ok `#40b956`, warn `#fecb00`, bad `#ff5d5e`, busy `#6a9cff`, shadow `0 .5px 1px 1px #0000004d`.
  - Quiet, grey, dense, hairline borders, small caps-free labels, monospace for IDs/logs.
- Status semantics: `info` neutral/busy, `ok` green, `warn` yellow, `error` red,
  `blocked` = needs the human (most prominent). Agent badge = status of agent's latest
  non-append change. "Needs you" strip at top = agents/posts in `error` or `blocked`.
- Agents grouped by first path segment (`lead`, `lead/tests` under `lead`). Each agent:
  name, badge, "now doing" (latest title or text), last update relative time, live log
  pulse. Filter the feed by agent. Feed most recently changed first (by `seq`).
- Renderers: text (pre-wrap), markdown (markdown-it, html:false, linkify → DOMPurify),
  table (sticky header, horizontal scroll, numeric right-aligned), chart (ECharts;
  defaults merged only where unset: theme colors from tokens for light/dark, transparent
  bg, tooltip axis/item, legend if ≥2 named series, dataZoom inside if >500 pts, lttb
  sampling + animation off if >2000; re-theme on color-scheme change; resize on width
  change; option is untrusted JSON—never eval), image (`<img loading=lazy>`, click to
  open full), log (monospace, auto-scroll to bottom when user is at bottom, live pulse
  while `live`, exit code chip, "truncated" note).
- 390px: single column, agents as horizontal chips/strip above feed. 1440px: sidebar of
  agents + feed (max content width ~960px). Live connection indicator (SSE state).
