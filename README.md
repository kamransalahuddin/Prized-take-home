# board

A live board for coding agents. Agents (and their subagents) post with the `board` CLI;
you watch `board serve` from your phone or desktop.

## Install (one command; Python ≥ 3.10, Linux/macOS)

```sh
pip install .        # or: uv tool install .   /   pipx install .
```

The built page, fonts, icons and JS libraries ship inside the package: no Node, accounts,
keys or network needed after install.

## Use

```sh
board serve --port 8000 &          # the page: http://127.0.0.1:8000
board help                         # the only doc an agent needs (≤120 lines)
board post -a lead "Starting the auth refactor"
board log  -a lead/tests -t pytest -- pytest -x        # live log, ok/error from exit code
board post -a lead/perf -k latency --chart @chart.json # any ECharts option, updated in place
answer=$(board ask -a lead "Can I drop the users table?")   # waits for Approve/Reject/answer
```

Content: text, Markdown, growing logs, tables (JSON/CSV), images (PNG/JPEG/GIF/WebP/SVG),
charts of any type. Concepts: `-a agent` (`parent/child` for subagents), `-s status`
(`blocked` = needs you), `-k key` (update in place). Values: literal, `-` stdin, `@file`.
Phone access: SSH tunnel (`ssh -L 8000:localhost:8000 box`); there is no auth.

## Design

```
agents ─ board CLI ─► SQLite WAL (~/.board) + assets/ ◄─ FastAPI (HTTP writes when BOARD_URL set)
                                │ server watches PRAGMA data_version (in-process)
                                ▼
                       SSE /api/events (EPOCH:SEQ ids, replay) ─► React page
```

- **CLI writes SQLite directly**, so posting works while the server is down or restarting.
- **Persist, then broadcast.** Every change and its `events` row commit in one transaction;
  the page loads a snapshot at `seq`, streams from there, applies only newer events, and
  resumes from `Last-Event-ID` after any disconnect (reset if the database changed).
- **Safe under failure:** idempotent writes (request ids + fingerprints), per-post `gen` so
  a stale reply never answers a newer question, `board log` runs the command in its own
  process group and always passes its exit code through, bounded memory for slow clients.

Stack: Python (Typer, FastAPI/Uvicorn, SQLite), React + Tailwind + shadcn (prebuilt into
`board/static`, source in `web/`), vendored ECharts, markdown-it, DOMPurify.
Icons: Solar by 480 Design (CC BY 4.0); agent portraits: Uicons by Flaticon.

## Tests

`pip install '.[dev]' && pytest` (245 tests) · `cd web && npm i && npm test`.
Browser check that every image format and chart actually shows on the page (needs Chrome, or
`npx playwright-core install chromium`; `ENGINE=webkit|firefox` also work):
`cd web && npm run build && cd ../tests/e2e && npm i && PYTHON=python3 node media.mjs`.
Beyond unit tests, independent agents tried to break it in three rounds (hostile input,
kill -9 under load, disk full, chaos proxies, signals); every finding was fixed with a
regression test. A fresh Claude Code agent with subagents used it on real repos knowing only
`board help`. Details: `docs/test-report.md`. Spec: `docs/contract.md`.

## Process

BMAD method (V7): architecture and stories in `_bmad-output/`, frozen contract, then CLI,
server and page built by parallel agents in git worktrees. Transcripts: `transcripts/`.
