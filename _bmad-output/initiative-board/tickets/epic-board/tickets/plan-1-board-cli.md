---
title: 'Agents post every content kind with the board CLI'
type: feature
ticket: '1'
created: '2026-11-18'
status: built
baseline_revision: '6b61865'
route: full
route_source: pinned
risk: medium
review: quick
review_source: auto
lenses_ran: [self-review]
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/docs/contract.md'
  - '{project-root}/board/store.py'
  - '{project-root}/board/help.py'
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** Agents need a CLI to post text, markdown, tables, charts, images and live logs to the
board from many separate processes; it is the only interface the grader's agent learns (from
`board help`), so it must do exactly what the help text promises.

**Approach:** Typer app in `board/cli.py` over a tiny backend abstraction (`LocalBackend` wrapping
`Store`, `HttpBackend` using HTTPX when `BOARD_URL` is set). `main()` owns help handling and maps
exceptions to exit codes. `log` / `append -` share one streaming engine (reader + 250 ms flusher).

## Boundaries & Constraints

**Always:** behavior matches `board/help.py` and contract §2; stdout carries only the ID / JSON /
URL; errors are `board: <msg>` on stderr; exit codes 0/1/2/3/child; httpx/fastapi/uvicorn imported
lazily; storage failure never kills a logged child or stops its echo.

**Never:** reimplement storage logic; edit `board/server.py`, `board/static/*`, `docs/contract.md`;
touch `~/.board` in tests.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| post text | `post -a lead hi` | ID on stdout, exit 0 | — |
| value rule | `-` / `@path` | stdin / file contents | TTY/empty stdin or missing file → exit 2 |
| two sources | `post -a x hi --md y` | — | exit 2 `use exactly one of ...` |
| bad chart JSON | `--chart '{'` | — | exit 2 with line/col |
| key upsert | same `-k` twice | same ID both times | — |
| log run | `log -a t -- sh -c '...exit 3'` | ID first, live chunks, status error, exit 3 | store failure → warn, keep echo |
| log pipe | `cmd \| board log -a t` | streams stdin, done at EOF | TTY stdin → exit 2 |
| append | `append ID TEXT` / `-s ok` | ID, text appended / status changed | bad ID → exit 1; nothing → exit 2 |
| BOARD_URL down | any write | — | exit 3 |

</intent-contract>

## Code Map

- `board/store.py` -- storage (reused as-is)
- `board/help.py` -- verbatim HELP text
- `board/cli.py` -- new: Typer app, backends, streaming engine, main()
- `tests/conftest.py` -- tmp BOARD_DIR + subprocess runner fixtures
- `tests/test_cli.py` -- CLI behavior incl. live log + 10-process concurrency
- `tests/test_store.py` -- store-level sanity (concurrency, append semantics)

## Tasks & Acceptance

**Execution:**
- [x] `board/cli.py` -- implement commands help/post/log/append/ls/serve + backends + main -- contract §2
- [x] `tests/conftest.py`, `tests/test_cli.py`, `tests/test_store.py` -- cover the matrix above -- story verify line

**Acceptance Criteria:**
- Given 10 concurrent processes each posting 10 times, when they finish, then 100 posts exist with unique, increasing seqs.
- Given `board log -- CMD`, while CMD is still running, then its earlier output is already in the store.

## Implementation Notes

- Typer >= 0.20 vendors click as `typer._click`; `run()` maps click exceptions by class
  name (Exit / UsageError / Abort) so it works with old and new Typer.
- Help is handled in `run()` before Typer parses (`help_option_names=[]`): bare `board`,
  `help`, and `-h`/`--help` anywhere before `--` print HELP.
- Agent and status are validated client-side so bad values exit 2 (bad command), not 1.
  Bad table input also exits 2; chart without "series" is a store rejection (exit 1).
- `log`: if creating the post fails, the command is still run (pipe mode: still drained and
  echoed) and the error is reported; run mode exits with the child's code. Killed child →
  128+signal. Command not found → 127 (126 not executable), recorded on the post.
- Streamer holds back a trailing partial ANSI escape and a lone trailing `\r` between
  batches so stripping / CR-collapsing in the store is not broken by chunk boundaries.
- `ls` fetches payloads only for untitled text/markdown/log posts (title fallback).
- `append ID -` validates the post kind first, then streams like log pipe mode (echo unless -q).
- store.py fix: `PRAGMA journal_mode=WAL` wrapped in `_retry` — concurrent first open of a
  fresh DB raised `database is locked` (caught by tests/test_store.py concurrency test).
- `serve` is written against `board.server.create_app(data_dir)`; not tested end-to-end.

## Plan Change Log

## Review Triage Log

- Pass 1 (self-review vs story verify line + contract §2): 0 high, 2 medium patched
  (pipe-mode log now drains/echoes when post creation fails; fresh-DB WAL race in store),
  0 open. Verify line satisfied: tests/test_cli.py green incl. 10-process concurrency and
  live log streaming checked mid-run.

## Verification

**Commands:**
- `.venv/bin/pytest` -- expected: all tests pass
