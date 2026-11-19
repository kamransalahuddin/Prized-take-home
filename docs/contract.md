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
  the HTTP API below (same semantics; exit 3 if unreachable).
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
  file (exit 2 `file not found: path`). `--image FILE` takes a path or `-` (bytes).
- Hidden aliases: `--text` (= positional TEXT), `--markdown` (= `--md`).
- `post`: exactly one content source, else exit 2 `use exactly one of TEXT, --md, --table, --chart, --image`.
  `-a/--agent` (fallback `BOARD_AGENT`), `-t/--title`, `-s/--status`, `-k/--key`, `--json`, `-q`.
  table → `store.normalize_table(value)`; chart → must parse as JSON object (exit 2 with
  line/col on bad JSON); image → `store.add_asset(bytes)` then post `{"asset_id": id}`.
- `log`: `board log -a NAME [-t T] [-s S] [-k K] [--json] [-q] [-- CMD ARGS...]`
  - Creates `kind="log", live=True` post first, prints ID (flush) immediately.
  - Run mode: spawn CMD (no shell), stderr merged into stdout, env `PYTHONUNBUFFERED=1`,
    default title = command line. Forward SIGINT/SIGTERM to child. On exit:
    `append(id, done=True, exit_code=rc)`; board exits with rc.
  - Pipe mode (no CMD): read stdin (exit 2 if TTY: `pipe output into board log, or use: board log -a NAME -- CMD`). EOF → `append(id, done=True)`.
  - Reader thread copies every byte to stderr (unless `-q`) and buffers; flusher sends
    `append(id, text)` every 250 ms or at 256 KB. Strip ANSI (`store.strip_ansi`).
    Store/HTTP failure never kills the child or stops the echo (buffer ≤10 MB, retry).
- `append ID [TEXT] [-s S] [-t T] [--json] [-q]`: TEXT literal / `-` / `@path`;
  one-off literal → `store.append(id, text, newline=True)`; `-` streams like log pipe mode.
  No TEXT and no -s/-t → exit 2.
- `ask -a NAME [-t T] [-k K] [--timeout SECONDS] [--json] QUESTION` (QUESTION: value rule):
  creates a `text` post, status `blocked`, `payload.ask = true`; stderr
  `board: waiting for the human on ID (board wait ID to resume)`; then waits like `wait`.
- `wait ID [--timeout SECONDS] [--json]`: waits until the post has `payload.reply`, then
  stdout line 1 = `approve` | `reject` | `answer`, then the reply text (if any) on the next
  lines (`--json`: one line `{"id","action","text","at"}`); exit 0. Already replied →
  prints at once. Not blocked and no reply → exit 1 `post is not waiting for input`
  (also if it stops being blocked without a reply while waiting). Polls the store every
  0.5 s, or with `BOARD_URL` `GET /api/posts/{id}` every 1 s, warning once and waiting on
  while the server is down. `--timeout` (default: forever) → exit 4, the question stays
  open. Ctrl-C → exit 130.
- `ls [-a NAME] [-n 20] [--json]`: `store.list_posts(agent, limit, with_payload=False)`;
  TSV lines `ID\tAGENT\tKIND\tSTATUS\tTITLE` (title fallback: first 60 chars of text, else `-`).
- `serve [--port 8000] [--host 127.0.0.1] [--data DIR]`: if port already serves a board
  (`GET /api/posts?limit=1` ok) print `board: already running at URL`, exit 0. Print
  `board: serving http://HOST:PORT  data: DIR` to stderr, URL to stdout. Warn on stderr
  when host is `0.0.0.0` (no auth; use an SSH tunnel). Runs uvicorn with
  `timeout_graceful_shutdown=2`, one worker.
- stdout: post ID + `\n` (or one-line JSON with `--json`). Everything else → stderr.
- Exit codes: 0 ok · 1 rejected (`BoardError`, HTTP 4xx/5xx) · 2 usage/input · 3 `BOARD_URL`
  unreachable · 4 `ask`/`wait` timed out with no reply · `log -- CMD` → CMD's code.
- Idempotent writes: the CLI sends a fresh uuid4 `request_id` with every logical write
  (post, append, each streamed log chunk, reply, asset) and reuses it on every retry of
  that write. A chunk being retried keeps its exact text; newer output goes in a later
  chunk. Over HTTP, one-shot writes retry transport errors up to 3 times.

## 3. Data model

Post object (from `store.get_post` / `list_posts`, in API responses and `post` events):

```json
{"id":"p_3f9k2a","agent":"lead/tests","kind":"text|markdown|table|chart|image|log",
 "title":"pytest or null","status":"info|ok|warn|error|blocked","key":"latency or null",
 "live":false,"created_at":"2026-11-18T16:45:12.345Z","updated_at":"...","seq":1042,
 "payload":{...}}
```

| kind | payload |
|---|---|
| text | `{"text": "..."}` |
| markdown | `{"markdown": "..."}` |
| table | `{"columns": ["a","b"], "rows": [["1", 2], ...]}` cells: str/number/bool/null |
| chart | `{"option": {ECharts option}}` |
| image | `{"asset_id":"a_9c1d7e2f","url":"/api/assets/a_9c1d7e2f","mime":"image/png"}` |
| log | `{"text":"...","truncated":false,"exit_code":null}` (last 2 MB kept) |

`seq` = id of the last event that touched the post. Global, monotonic.

Human replies (any non-log kind, normally `text`):
- `payload.ask = true` — made by `board ask` (the page shows the reply buttons;
  a post made with `-s blocked` without it can be answered too).
- `payload.reply = {"action": "approve"|"reject"|"answer", "text": str|null,
  "at": "2026-11-18T16:45:12.345Z"}` — set once by `POST /api/posts/{id}/reply`
  (`Store.reply`); the same write sets status `blocked` → `info`. A `--key` replace
  replaces the payload, so it clears the reply (re-asking with the same key works).

Idempotency: table `requests(id, result, created_at)`. `create_post`, `append`, `reply`
and `add_asset` take an optional `request_id`; in the same transaction, a known id returns
the stored result without writing (an id reused for a different operation → 409). Rows
older than 1 day are pruned on write.

## 4. HTTP API (server.py; thin wrapper over Store; errors → `{"error": msg}` with `BoardError.status`)

| Method/path | Body → Response |
|---|---|
| `POST /api/posts` | `{"agent","kind","payload","title"?,"status"?,"key"?,"live"?,"request_id"?}` → 201 post+`"created":true` / 200 on key replace (`"created":false`). Body limit 8 MB (413). |
| `POST /api/posts/{id}/append` | `{"text"?,"status"?,"title"?,"done"?,"exit_code"?,"newline"?,"request_id"?}` → 200 `{"id","seq","status","title","live","updated_at","size"}`; 404 / 409 |
| `POST /api/posts/{id}/reply` | `{"action":"approve"\|"reject"\|"answer","text"?,"request_id"?}` (`answer` needs non-empty text; text ≤ 10000 chars) → 200 full post (`payload.reply` set, status `info`, emits `post`); 400 bad action/text · 404 · 409 `{"error":"already answered"}` / `{"error":"post is not waiting for input"}` (status not `blocked`) |
| `POST /api/assets` | multipart field `file` (+ optional form field `request_id` or header `X-Request-Id`) → 201 `{"id","mime","size","url"}`; 415 / 413 |
| `GET /api/posts?agent=&limit=200` | → `{"posts":[...], "seq": N}` (N consistent with posts) |
| `GET /api/posts/{id}` | → post |
| `GET /api/assets/{id}` | bytes, stored MIME, `Cache-Control: public, max-age=31536000, immutable`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox` |
| `GET /api/events` | SSE, see §5 |
| `GET /` | `board/static/index.html`; `/static/*` serves `board/static/` (incl. `vendor/`) |

`request_id` (optional string ≤ 200 chars) on any write makes it idempotent: repeating the
same request returns the first response's body and writes nothing (see §3).

## 5. SSE wire format (`GET /api/events`)

Resume point = `Last-Event-ID` header (browser sends it automatically on reconnect) or
`?since=N` query (used on first connect, N = `seq` from `GET /api/posts`).

```
retry: 1000

event: hello
data: {"seq": 1041}

id: 1042
event: post
data: {full post object, incl. "seq"}

id: 1043
event: append
data: {"id":"p_3f9k2a","agent":"lead/tests","kind":"log","text":"chunk\n","status":"info",
       "title":"pytest","live":true,"truncated":false,"exit_code":null,
       "updated_at":"...","seq":1043}

: ping            (every 15 s)
```

- Server: register subscriber queue FIRST, then replay `events_after(since)` in batches,
  then stream live; drop any event with id ≤ last sent (dedupe). If `since` is older than
  `min_event_id()-1` (shouldn't happen; events are never deleted) send `event: reset`.
- `post` = create / key replace / status-or-title-only change (full object).
  `append` = text chunk + current status/title/live (a `done` with text is an append with
  `live:false`; a `done` without text is a `post`).
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
  pulse. Filter the feed by agent. Feed newest-updated first.
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
