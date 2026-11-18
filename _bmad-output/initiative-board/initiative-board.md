---
id: 1
type: initiative
title: "Coding agents report live to a board a person reads from any device"
sources: ["docs/contract.md"]
companions: ["_bmad-output/initiative-board/architecture-board/architecture.md"]
after: []
risk: medium
---

# Coding agents report live to a board a person reads from any device

## Description

Prized take-home. A person starts a coding agent on a remote box and walks away; the agent
and its subagents post progress, findings, failures and requests for help to `board`, and
the person watches a live page from a phone or desktop.

## Outcome

The person can answer "what is each agent doing, what has it found, is anything failing,
does it need me" within seconds, from a phone.

## Requirements

- R1: CLI is small and generic; `board help` alone teaches every feature.
- R2: Rich content: text, Markdown, growing logs, tables, images (PNG/JPEG/GIF/WebP/SVG), charts of any kind (thousands of points, many series).
- R3: Many agents/subagents post concurrently from separate processes; the page shows who does what.
- R4: Durable: restarting `board serve` loses nothing.
- R5: Live: new posts appear within ~1 s, server push, no polling.
- R6: Good at 390 px and 1440 px, light/dark following the OS, looks like prized.dev/dashboard.
- R7: One install command, Linux/macOS, no accounts/services/keys, fully offline once installed.

## Done when

- A fresh Claude Code agent, given only `board help`, reports a multi-subagent task on the board correctly.
- Restart and offline checks pass; screenshots at 390/1440 light/dark look right.

## References

- architecture — _bmad-output/initiative-board/architecture-board/architecture.md
- contract — docs/contract.md
