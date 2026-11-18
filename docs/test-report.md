# board — test report (CLI + backend)

Scope: the `board` CLI, `store.py` (SQLite) and `server.py` (REST + SSE). The page is being
rebuilt separately and is not covered. Final state: **all six areas pass; 90 automated
tests green** (`.venv/bin/pytest`).

## Process

1. **One validation agent** ran every command and every example in `board help` the way a
   first-time agent would, then went through every flag, input form and exit code.
2. **Five agents in parallel**, one per property: offline persistence, live streaming,
   in-place updates, concurrent agents (20 writers), restart recovery.
3. **Isolation:** each agent got its own temp `BOARD_DIR` and its own port, kept its scripts
   in a scratch folder, and was told to test only, with no code edits. A real Python SSE
   client recorded what a browser would receive.
4. **Every bug** was fixed on `main`, got a regression test, and was re-checked by hand.

| Area | Result | Bugs found → fixed |
|---|---|---|
| CLI commands | PASS after fixes | 3 medium, 6 low → all medium and 5 low fixed |
| Offline persistence | PASS after fixes | 2 → fixed |
| Live streaming | PASS | 0 (1 help note added) |
| In-place updates | PASS after fixes | 2 low → fixed |
| Concurrent agents (20) | PASS | 0 |
| Restart recovery | PASS after fixes | 3 → fixed |

## 1. CLI commands — every command and help example

**How.** Ran every line of `board help` literally, then each command against these inputs:
- `post`: all six content kinds, every value form (literal, `-` for stdin, `@file`), every
  image type plus a non-image, invalid JSON, zero or two content sources;
- `-t`, `-s`, `-k`;
- `log` in run and pipe mode, including signals, command not found, ANSI codes, `\r`, and
  200k lines;
- `append`, `ls` and `serve`, including a second start and a busy port;
- every help entry point, `--json`, `-q`, `BOARD_AGENT`, `BOARD_URL` (live server and dead
  port);
- every exit code.

**Result.** Every help example works as written. Speed: `board post` takes about 68 ms
writing to SQLite directly and about 124 ms over HTTP; `board help` takes 36 ms.

**Bugs, all fixed:**
- **(med)** Text starting with `-` was read as an option: `board post -a lead "-3 failed"`,
  `board append ID "- item"`.
- **(med)** `"@human approve"` was always treated as a file path. Now `@` text posts as-is
  unless it names a file or looks like a path.
- **(med)** `serve` on a busy port printed a URL, then a uvicorn error, and exited 3. Now it
  gives a clear message and exits 1.
- **(low)**
  - `--port 99999` printed a traceback.
  - A chart with no `series` exited 1 instead of 2.
  - `--table 'text'` made an empty table.
  - `serve` rejected `--json` and `-q`.
  - An unreachable `BOARD_URL` gave no fix hint.

**Help wording also fixed:** text starting with `-` or `@`; set `ok` after resolving a
blocked post; a pipe log stays `info`; `ls -a` includes subagents; what exit code 1 covers.

**Left as is (low):** `append ID -` with empty stdin succeeds, and `-n 0` is treated as 1.

## 2. Offline persistence

**How:**
- With the server **not running**, posted every kind, two logs, a key update, an append and
  a status change.
- Checked the rows and asset files with a fresh `sqlite3` connection, then started the
  server and compared `GET /api/posts`.
- Checked that a client replaying from `?since=0` gets every event, including 1,100 posts
  written by 8 parallel writers.
- Searched everything the server serves for external URLs.
- Ran the server and CLI under a macOS `sandbox-exec` profile that **blocks all outbound
  network** except localhost.
- Ran `kill -9` on a `board log` mid-run.

**Result: PASS.**
- With the server down, every command exited 0, and every post appeared complete once the
  server started.
- Images came back byte-identical.
- The replay was complete and in order (1,115 contiguous events).
- With the network blocked, posting, serving and all three vendored JS libraries still
  worked.
- No runtime calls to a CDN, fonts or analytics, and FastAPI's docs pages (which load a CDN)
  are turned off.
- After `kill -9`, all output except the last ~0.25 s was kept and `integrity_check` passed.

**Bugs, both fixed:**
- **(med)** `board serve` said "already running" when that server used a *different* data
  folder, so the agent's posts would never appear. The server now reports its data folder,
  and `serve` refuses on a mismatch.
- **(med)** A `board log` killed with `-9` stayed "live" forever. Logs now record their
  writer's process, and the server closes orphaned logs every 5 s, marking them `error` with
  a note. Verified: a killed log was closed about 6 s later.

## 3. Live streaming

**How:**
- An SSE client recorded the receive time of each event.
- **50 posts:** measured from starting the CLI process to the event arriving.
- **Log streaming:** a child printed a timestamped line every 100 ms for 5 s. Measured from
  each print to its arrival, in run and pipe mode, with and without flushing.
- **Correctness:** checked that the appended chunks join up to the stored text exactly (with
  multi-byte UTF-8 split across batches, and a 3.4 MB log).
- **Push, not poll:** counted connections, checked headers and timed keepalive pings.
- **Scale:** 5 clients at once; a burst of 200 posts from 4 parallel loops.

**Result: PASS.**
- **New posts:** 122 ms median and **170 ms p95** (target < 1 s).
- **Log lines:** arrive while the command runs, 170 ms median and 300 ms p95.
- **One long-lived connection per client**, with `retry: 1000`, a `hello` event and
  `: ping` every 15 s.
- **5 clients** received identical streams.
- **The 200-post burst** arrived complete, contiguous and in order.
- **Status changes** arrive in about 100 ms.

**Notes:**
- A program that buffers its piped output only shows up when it exits. That's the program's
  buffering, and the help now says to use `-- CMD` or `python -u`.
- A log over the 2 MB cap can start mid-line. Cosmetic.

## 4. In-place updates

**How.** With the server and an SSE recorder running:
- 20 `-k` updates of one post;
- chart updates growing to 5,000 points;
- changing kind under one key (chart → table → markdown, and log → text);
- key scoping across agents;
- text, markdown and log appends;
- status-only and title-only changes;
- **a race:** 2×50 CLI processes and 100 HTTP requests updating the same key;
- rebuilding every post from `?since=0` using the "apply only if `seq` is newer" rule and
  comparing it with the snapshot.

**Result: PASS (9/9).**
- **Updates:** same ID every time, exactly one row, `created_at` unchanged, `seq` strictly
  increasing.
- **Kind change:** an old log's text does not leak into its replacement.
- **Race:** one row, no errors, and the final state matches the event with the highest
  `seq`.
- **Rebuild:** all 16 posts rebuilt from events match the snapshot exactly.

**Bugs, both fixed:**
- **(low–med)** A running `board log` kept writing into its post after `-k` replaced that
  post with another kind. The log now stops posting; the command keeps running and its exit
  code still comes through.
- **(low)** `append ID ""` was a silent no-op that still bumped the post. Now exit 2.

## 5. Concurrent agents — 20 writers at once

**How.**
- A driver started **20 agent processes on a barrier** (start spread 2–4 ms), named `lead`
  and `lead/a01`…`lead/a19`.
- Each agent made 25 mixed posts (including 1,000-point charts and CSV from files), 1 image,
  3 `-k` updates, a 50-line streaming log and 3 appends, all through real `board` calls.
- Checked:
  - every exit code and all stderr;
  - exact counts of rows, kinds, log lines and asset files;
  - event ids contiguous, with each post's `seq` equal to its last event;
  - each agent's own ordering;
  - the SSE client got every event exactly once, in order;
  - the snapshot equals the database;
  - `integrity_check`.
- Ran 3 times, plus a 2,000-write burst and both again over HTTP.

**Result: PASS, no bugs.**

| Run | Ops | Errors | Wall | p95 per call | SSE |
|---|---|---|---|---|---|
| Mixed ×3, direct to SQLite | 20×33 (~920 events) | 0 | ~10 s | 306–370 ms | all exact, last event ≤ 85 ms after the final write |
| Burst, direct | 20×100 = 2,000 | 0 | 22 s | 327 ms | exact |
| Mixed, HTTP | 20×33 | 0 | 18 s | 655 ms | exact |
| Burst, HTTP | 2,000 | 0 | 41 s | 534 ms | exact |

It never hit "database is locked". Most of each call's time is 20 Python processes starting
at once, not waiting on SQLite.

## 6. Restart recovery

**How:**
- SIGTERM and SIGINT with 3 open SSE clients.
- Compared the full snapshot before and after a restart.
- `kill -9` during 10 writers × 30 posts.
- 30 posts while the server is down, then reconnecting with `Last-Event-ID` or `?since=`.
- An EventSource-like client through 3 restarts (SIGTERM, `kill -9`, SIGINT) while posts and
  a log stream in.
- A 10 s `board log` across 2 restarts.
- `BOARD_URL` writers during an outage.
- `kill -9` during 15 MB uploads.
- `reset` on a wiped database.

**Result: PASS after fixes.**
- **Shutdown and restart:**
  - Graceful stop takes 0.15–0.23 s.
  - The snapshot is identical after restart.
  - After `kill -9`, all 300/300 posts are there with contiguous events and a passing
    `integrity_check`.
- **Catch-up after downtime:**
  - The reconnecting client got **1,571/1,571 events, with no gaps or duplicates**.
  - Missed-event replay is exact.
- **Logs and uploads:**
  - A log across restarts is complete and keeps its exit code.
  - No database row ever points at a half-written image.

**Bugs, all fixed:**
- **(high)** After `kill -9`, or after a stop while a client held a connection open,
  `board serve` refused to restart for about 30 s ("Address already in use"). My earlier
  busy-port check caused this, because it didn't set `SO_REUSEADDR` the way uvicorn does.
  Now it does; regression test added.
- **(med)** In `BOARD_URL` mode, a log whose server was down when the command ended gave up
  after about 2.5 s and lost the end of its output. It now keeps retrying for up to 60 s;
  regression test added (server down 2.5 s → all lines and the exit code delivered).
- **(low)** Half-written upload files left by a crash were never removed. The server now
  deletes leftovers older than 10 minutes when it starts.

## Known limitations (accepted)

- **Durability:** `synchronous=NORMAL` survives process crashes, but the last commits could
  be lost on power loss or an OS crash.
- **Ordering ties:** `updated_at` has millisecond resolution. Ties are broken by `seq`, so
  ordering stays correct.
