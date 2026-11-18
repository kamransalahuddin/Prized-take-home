# Architecture — board

Spine for initiative-board. Interface details are frozen in `docs/contract.md`; this file
records the system shape and the decisions behind it.

## System

```
Coding agents / subagents ── board CLI (Typer) ──► store.py ──► SQLite WAL + assets/
                                                                   ▲
Remote agents ── HTTP (HTTPX when BOARD_URL set) ──► FastAPI ──────┘
                                                      │ watcher (PRAGMA data_version, 100 ms, in-process)
                                                      ▼
                                              SSE /api/events ──► browser (vanilla JS,
                                              markdown-it + DOMPurify, ECharts; all vendored)
```

| Layer | Technology |
|---|---|
| CLI | Typer (+ HTTPX for remote) |
| Backend | FastAPI + Uvicorn, one worker |
| Storage | SQLite WAL (posts, log_chunks, assets, events) + local filesystem for images |
| Live | Server-Sent Events, Last-Event-ID replay from `events` |
| Frontend | Vanilla JS/CSS, no build step |

## Decisions

- **AD-1 CLI writes SQLite directly.** Posts survive the server being down or restarting;
  no lost work when `board serve` isn't up yet. The HTTP API stays for remote writers.
  The server notices other processes' commits via `PRAGMA data_version` (server-side check,
  not browser polling) and broadcasts from the `events` table.
- **AD-2 Persist before broadcast.** Every mutation writes its rows and one `events` row in
  one `BEGIN IMMEDIATE` transaction; broadcasting reads committed events only.
- **AD-3 Global `seq` = `events.id`.** Each post stores the seq of its last change; the page
  applies an event only if its seq is newer → snapshot + stream can't double-apply.
- **AD-4 SSE, not WebSockets.** Traffic is one-way. Browser `EventSource` reconnects and sends
  `Last-Event-ID` itself; server registers the subscriber before replaying, dedupes by id.
- **AD-5 Single Uvicorn worker.** In-memory fan-out is enough; no broker.
- **AD-6 Status is a post attribute** (`info|ok|warn|error|blocked`). An agent's badge is
  its latest status; `blocked` means "needs the human". No separate status concept.
- **AD-7 `--key` upsert** per agent for things that update (progress, charts) — same id.
- **AD-8 Charts are ECharts option JSON**, so no fixed set of chart types; the page merges
  theme/tooltip/legend/zoom/sampling defaults only where the agent left them unset.
- **AD-9 Assets outside SQLite**: magic-byte sniffed, written to a temp file, fsynced,
  atomically renamed; SVG only rendered via `<img>` and served with a sandbox CSP.
- **AD-10 `board help` is hand-written plain text** (Typer Rich help disabled) — it is
  the only documentation the grading agent reads.
- **AD-11 Data dir `~/.board`** (`BOARD_DIR`): independent of the agent's cwd, never
  committed by accident.
