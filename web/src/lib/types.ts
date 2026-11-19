// Shared types (contract: docs/contract.md §3–5). Data layer and UI both build on these.

export type Status = "info" | "ok" | "warn" | "error" | "blocked"
export type Kind = "text" | "markdown" | "table" | "chart" | "image" | "log"

export interface Post {
  id: string
  agent: string // "lead" or "lead/tests"
  kind: Kind
  title: string | null
  status: Status
  key: string | null
  live: boolean
  created_at: string
  updated_at: string
  seq: number
  gen?: string // changes on create and every --key replace (reply sends it as expect_gen)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  payload: Record<string, any>
}

/** What the home page shows under a card. */
export type AgentState = "needs-input" | "attention" | "running" | "done"

export interface AgentNode {
  id: string // full agent path, e.g. "backend/db-integration"
  name: string // generated first name, stable per id ("Rachel")
  role: string // humanized last segment ("DB integration")
  avatar: string // Solar icon name, stable per id ("ghost-bold")
  state: AgentState
  status: Status // status driving `state`
  live: boolean // has a live (streaming) log right now
  lastActivity: string // ISO time of newest change
  latest: Post | null // newest post (for a "now doing" line in the detail view)
  posts: Post[] // this agent's posts, newest first (not including children)
  // Open questions (blocked, unanswered, any kind) answerable on this agent's page, newest
  // first: its own, or, for a parent that never posted, its subagents'.
  questions: Post[]
  // The post behind `state`: the newest open question (needs-input) or the warn/error post
  // (attention); a parent that never posted takes its most urgent subagent's. Else null.
  cause: Post | null
  children: AgentNode[] // subagents, same shape
}

export type Connection = "connecting" | "live" | "offline"

export interface BoardState {
  posts: Post[] // all posts, newest updated first
  agents: AgentNode[] // top-level agents with nested children
  connection: Connection
  loaded: boolean
}
