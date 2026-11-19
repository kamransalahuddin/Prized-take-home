# board: test report

Scope: the `board` CLI, `store.py` (SQLite), `server.py` (REST + SSE), and the page wired to
them. Final state: 245 Python tests and 30 frontend unit tests pass. Every finding listed
below was fixed and has a regression test, unless it is marked as accepted.

## Method

1. **Unit and integration tests** (`pytest`, `vitest`) written alongside each change.
2. **Break-it rounds.** One test agent per area, each with its own temp `BOARD_DIR` and its
   own ports. The agents only test (no code edits) and report each finding with a repro
   and the code location. A fix agent fixes the findings on a branch with regression
   tests. I merge, and a fresh test agent for the same area re-checks every earlier finding
   and then attacks again. This repeated until every area came back clean.

   | Area | Round 1 | Round 2 | Round 3 |
   |---|---|---|---|
   | CLI commands, docs, hostile input | 3 med, 6 low, then 2 high, 9 med, 9 low | 1 high, 2 med, 9 low | clean (8 low, fixed) |
   | Crash recovery | 2 high, 5 med, 4 low | 2 med, 4 low | clean (2 low, fixed) |
   | Duplicate writes | 3 high, 3 med, 1 low | 1 high, 1 med | clean |
   | Network interruptions | 3 high, 2 med, 2 low | 2 high, 3 low | clean |
   | Safe termination | 4 high, 3 med, 4 low | 4 med, 4 low | 2 med, 2 low (fixed); round 4: 1 med, 2 low (fixed) |

3. **Browser end-to-end** (Playwright with the system Chrome) against the real server:
   1440×900 and 390×844 viewports, light and dark mode.
4. **Fresh-agent tests**, run as the graders will run them: a Claude Code agent with
   subagents does a real task on a real repo, knowing only that `board help` exists.

## Results by requirement

| Requirement | How it was tested | Result |
|---|---|---|
| CLI documented and functional | Every line and example in `board help` run literally. Every flag, value form (literal, `-`, `@file`), content kind, exit code (0/1/2/3/4, the child's code), and `BOARD_URL` mode. About 600 hostile raw-API requests (types, sizes, unicode, nesting, NaN/overflow, path traversal). | No tracebacks, no 500s, and help matches behavior. `board post` takes about 70–90 ms. |
| Rich content | All 6 kinds rendered in the browser: line, bar, scatter and pie charts (6 series × 5,000 points, plus a 20k-point chart); PNG, JPEG, GIF, WebP and SVG; a 50k-row table; a 2 MB log; sanitized Markdown. | All render. The SVG's script does not run, `<script>` and `javascript:` links are neutralized, there are no console errors, and no request leaves the machine. |
| Many agents at once | 20 agent processes on a barrier (mixed posts, images, upserts, logs, appends) ×3 runs, plus a 2,000-write burst, both direct and over HTTP. 50 processes racing on one request id or one key. | 0 errors, exact counts, contiguous `seq`, and every event delivered once and in order. No "database is locked". |
| Durable | Server killed (SIGTERM / `kill -9`) during writer bursts, uploads, migrations, pruning and replay. Posting while the server is down. 300+ random `kill -9` runs per round. Disk full (5 MB volume), read-only and deleted data dirs, corrupted WAL, databases from older versions. | No committed post was ever lost. `PRAGMA integrity_check` passed every time. Old databases migrate, even when killed mid-migration. |
| Live, pushed, no polling | SSE latency measured from the CLI call to the event: p95 170 ms for posts, about 300 ms for log lines. Browser: a new agent appears in about 170 ms and a status change in about 90 ms. | Pass. |
| Recover missed updates | A chaos proxy (drops, mid-frame cuts, half-open stalls, throttling) for 10+ minutes with 7 browser-like clients. 35 `kill -9` restarts in 3 min. Event pruning racing slow clients. A wiped or replaced database. 300 simultaneous reconnects. | Every client ended with exactly the full event sequence and a state equal to the snapshot, or got a `reset` when it was beyond the kept window. No silent gaps. |
| No duplicate writes | A chaos proxy that drops responses, delays past timeouts, resets, sends requests twice and forwards late: 11,603 operations in round 3. | `BAD: {}`. A write whose answer is lost exits 1 "may already have been applied", never exit 3. |
| Safe termination | Every signal (INT, TERM, HUP, QUIT, KILL) to every command at random moments: storms, a pty Ctrl-C, grandchildren, children that ignore signals, closed stdout, `serve` with 300 clients. | No orphans (process groups), no hangs, the post is closed with its exit code, `serve` exits 0 in under 0.6 s and the WAL is checkpointed. |
| Phone and desktop, light and dark | Screenshots of the home page, Activity and every detail view at 390 and 1440 wide, in both schemes. Long names, deep nesting, 60 agents. | No horizontal overflow, and the data shown matches the database. |
| Offline, one install command | `pip install .` from a clean clone into a fresh venv, then serving the page. A server and CLI run under a macOS sandbox that denies all outbound network. | Works. All assets are served locally. |

## Major bugs found and fixed (selection)

- **One malformed post took the page down for everyone.** NaN/Infinity and ~10k-deep JSON
  made `GET /api/posts` return 500. Both are now rejected at write time, with a depth cap of
  64.
- **Duplicates under retries:**
  - a reused request id with a different body returned another post;
  - the CLI gave up while a delayed write could still commit;
  - a 5xx error killed log streams.

  Writes now carry fingerprinted request ids, are retried with the same id, and transient
  and permanent errors are told apart.
- **Wrong question answered.** A stale Approve could land on a newer question re-asked under
  the same `-k`. Posts now carry a `gen`; replies and streams send `expect_gen`, and a
  replay never adopts a newer post.
- **`board log` safety:**
  - it didn't run the command when storage failed;
  - it hung on backgrounded grandchildren;
  - signals orphaned children;
  - pipe mode lost buffered output.

  The command now runs in its own process group, finishes when the child exits, and the
  signal handlers are async-signal-safe.
- **Missed updates:**
  - a slow replay could be overtaken by event pruning;
  - the live watcher skipped pruned events;
  - a wiped database went unnoticed.

  The server now checks every batch, uses `EPOCH:SEQ` event ids, and sends `reset`.
- **Unbounded memory and disk:**
  - status changes re-sent 2 MB logs (3.45 GB RSS);
  - stalled clients were never cut off;
  - the event log had no limit.

  Events are now bounded by bytes, and each client has a byte budget and a send timeout.

## Fresh-agent runs (the graders' scenario)

1. **itsdangerous audit:** a lead agent plus 3 subagents (tests, size, review). They posted a
   plan, live pytest logs, timing tables and charts, findings marked `warn`, and a summary.
   The page, left open without reloading, matched the database at every 20 s frame. The
   agent's feedback on `board help` was applied: the `@` rule, `-k` scope, `log` options,
   the badge rule, and `ls` columns.
2. **python-slugify fix** (`board` on PATH, default `~/.board`, told only "run board help"):
   a lead plus 2 subagents, 0 failed board commands. It fixed 2 real bugs and added 17
   tests. When its subagents died, it posted a `warn` itself. Feedback applied: colour codes
   are stripped on the board, and `append -s ok` also resolves `warn` posts.

## Accepted limitations

- `synchronous=NORMAL` survives process crashes. A power loss or OS crash could lose the
  last commits.
- Children that call `setsid`, double-fork or use job control escape the process group.
- A signal that arrives while Python is still starting, before `board` has created a post or
  started a child, kills it by default. Nothing exists yet, so nothing is lost.
- `kill -9` of `board log` leaves its child running, because macOS has no parent-death
  signal. The post is reaped as `error`.
- No auth. Use an SSH tunnel for phone access.
