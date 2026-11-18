---
title: 'Server serves the API and pushes events over SSE'
type: feature
ticket: '2'
created: '2026-11-18'
status: built
baseline_revision: '6b61865'
route: full
route_source: auto
risk: medium
review: quick
review_source: auto
lenses_ran: [self-review]
review_loop_iteration: 0
followup_review_recommended: false
context: ['{project-root}/docs/contract.md', '{project-root}/board/store.py']
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** Agents write straight to SQLite through `Store` (CLI) or over HTTP; browsers need a REST snapshot plus a live push of every committed change within ~1 s, with lossless resume after reconnects.

**Approach:** `board/server.py` = FastAPI thin wrapper over `Store` (contract §4) plus one background watcher that polls `PRAGMA data_version` every 100 ms and fans committed events out to per-client bounded queues streamed as SSE (contract §5).

## Boundaries & Constraints

**Always:** reuse `Store` for every read/write; broadcast only from the watcher (single path, persist-before-broadcast); register the SSE subscriber before replay and dedupe by id; JSON errors `{"error": msg}` with `BoardError.status`; blocking DB work off the event loop; separate connections for watcher / HTTP reads / HTTP writes (writer commits must change the watcher's `data_version`), each guarded by a lock.

**Never:** browser polling; new dependencies (plain `StreamingResponse`); edits to `board/cli.py`, `board/static/*`, `docs/contract.md`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| create | POST /api/posts valid | 201 post + created:true | — |
| key upsert | same agent+key again | 200, same id, created:false | — |
| body too big | > 8 MB | 413 `{"error"}` | streamed read aborts early |
| append missing / wrong kind | bad id / table post | 404 / 409 | BoardError.status |
| non-image upload | text bytes | 415 | — |
| CLI write | separate Store commits | SSE `post` within 1 s | — |
| reconnect | Last-Event-ID: N | exactly events > N, in order, no dups | — |
| stale/foreign since | since < min-1 or > current | `event: reset` then live | — |
| slow client | queue > bound | stream ends; client reconnects + replays | — |
| shutdown / disconnect | SIGINT / client close | generators exit, queue removed | — |

</intent-contract>

## Code Map

- `board/store.py` -- storage API reused as-is
- `board/server.py` -- new: app factory, endpoints, watcher hub, SSE
- `tests/test_server.py` -- new: REST via TestClient, SSE + durability against real uvicorn

## Tasks & Acceptance

**Execution:**
- [x] `board/server.py` -- `create_app(data_dir)`, `run(host, port, data_dir)`, endpoints, `Hub` watcher + fan-out, SSE generator -- contract §4–5
- [x] `tests/test_server.py` -- REST cases, asset headers/415, SSE cross-process latency, Last-Event-ID replay, `?since=`, hello, restart durability -- story verify line

**Acceptance Criteria:**
- Given the server is running, when another process writes through its own `Store`, then the event appears on an open SSE stream within 1 s.
- Given a client reconnects with `Last-Event-ID`, then it receives exactly the missed events once, in order.
- Given SIGINT, then uvicorn exits without waiting on open SSE streams.

## Implementation Notes

- Three Store connections (watcher / locked reader / locked writer). Writer must differ from the watcher: `PRAGMA data_version` ignores the connection's own commits. HTTP writes also set a `nudge` event so the watcher polls immediately.
- Per-client queue is unbounded internally so a close sentinel always fits; the 5000-event bound is enforced in `Hub.publish` (overflow -> client dropped, reconnects with Last-Event-ID).
- Disconnect: uvicorn reports ASGI spec 2.3, so Starlette's StreamingResponse listens for `http.disconnect` and cancels the generator; `finally` unsubscribes.
- Shutdown: uvicorn waits for open responses before lifespan shutdown, so lifespan startup chains onto uvicorn's SIGINT/SIGTERM handlers and closes all streams (measured 0.18 s with an open stream). Works with plain `uvicorn.run(create_app(...))` on the main thread; `timeout_graceful_shutdown=2` remains the backstop otherwise.
- Extension beyond contract: `since` greater than the current seq (client from another/wiped DB) also gets `event: reset`. No `since`/Last-Event-ID -> live only from current seq.
- No store.py changes.

## Plan Change Log

## Review Triage Log

- Pass 1 (self-review vs story verify line + contract §1/3/4/5): high 0 / medium 0 / low 1. Low: `live`/`done`/`newline` use Python truthiness (string "false" is truthy) -- accepted, CLI sends JSON booleans.

## Verification

**Commands:**
- `.venv/bin/pytest tests/test_server.py -q` -- expected: all pass
