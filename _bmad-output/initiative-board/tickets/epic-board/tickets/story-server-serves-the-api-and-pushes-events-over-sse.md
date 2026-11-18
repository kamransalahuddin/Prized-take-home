---
id: 2
type: story
title: "Server serves the API and pushes events over SSE"
parent: epic-board
covers: [R4, R5]
after: []
hitl: false
risk: medium
---

# Server serves the API and pushes events over SSE

## Description

Implements board/server.py per docs/contract.md sections 4-5: REST, assets, SSE with Last-Event-ID replay, data_version watcher.

## Requirements

Verify: pytest tests/test_server.py passes, incl. a write made by a separate Store appearing on an SSE stream within 1 s and replay after reconnect.

## References

- parent — _bmad-output/initiative-board/tickets/epic-board/epic-board.md
