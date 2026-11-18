---
id: 1
type: epic
title: "board CLI, server and live page"
parent: initiative-board
covers: ["R1", "R2", "R3", "R4", "R5", "R6", "R7"]
sources: ["docs/contract.md"]
companions: []
after: []
risk: medium
---

# board CLI, server and live page

## Description

Deliver the three parts on the shared, already-implemented `board/store.py`: the agent CLI,
the FastAPI/SSE server, and the vanilla-JS page; then integrate and harden. The contract in
`docs/contract.md` is frozen so the three stories can be built in parallel worktrees.

## Requirements

- R1..R7 (parent R1..R7), as written in the initiative.

## Done when

- `pip install .` then `board help`, `board serve`, all post kinds from 4 concurrent agents visible live.
- pytest green: store concurrency, CLI, SSE replay with Last-Event-ID, restart durability.
- README ≤ 1 page.

## Boundaries

- Story 1 owns `board/cli.py` + CLI tests; story 2 owns `board/server.py` + server tests;
  story 3 owns `board/static/*` (except vendor). `board/store.py`, `board/help.py` and
  `docs/contract.md` change only through the orchestrator.

## References

- parent — _bmad-output/initiative-board/initiative-board.md, section Requirements
- architecture — _bmad-output/initiative-board/architecture-board/architecture.md
- contract — docs/contract.md
