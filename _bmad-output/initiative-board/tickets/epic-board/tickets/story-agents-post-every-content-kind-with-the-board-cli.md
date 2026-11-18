---
id: 1
type: story
title: "Agents post every content kind with the board CLI"
parent: epic-board
covers: [R1, R2, R3, R4]
after: []
hitl: false
risk: medium
---

# Agents post every content kind with the board CLI

## Description

Implements board/cli.py per docs/contract.md section 2 on top of board/store.py, with tests.

## Requirements

Verify: pytest tests/test_cli.py passes, incl. 10 concurrent processes posting with no loss and live log streaming.

## References

- parent — _bmad-output/initiative-board/tickets/epic-board/epic-board.md
