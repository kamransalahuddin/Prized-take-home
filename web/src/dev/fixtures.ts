// Realistic board states for local development and screenshots (mirrors the home page sketch).
import type { AgentNode, AgentState, BoardState, Kind, Post, Status } from "@/lib/types"

const NOW = Date.parse("2026-11-18T15:42:00Z")
let seq = 0

function ago(minutes: number) {
  return new Date(NOW - minutes * 60_000).toISOString()
}

function post(
  agent: string,
  kind: Kind,
  title: string,
  status: Status,
  minutes: number,
  live = false,
  payload: Record<string, unknown> = {},
): Post {
  seq += 1
  return {
    id: `p_${seq.toString().padStart(4, "0")}`,
    agent,
    kind,
    title,
    status,
    key: null,
    live,
    created_at: ago(minutes + 3),
    updated_at: ago(minutes),
    seq,
    payload,
  }
}

function agent(
  id: string,
  name: string,
  role: string,
  avatar: string,
  state: AgentState,
  status: Status,
  posts: Post[],
  children: AgentNode[] = [],
): AgentNode {
  return {
    id,
    name,
    role,
    avatar,
    state,
    status,
    live: posts.some((p) => p.live),
    lastActivity: posts[0]?.updated_at ?? ago(60),
    latest: posts[0] ?? null,
    posts,
    questions: posts.filter((p) => p.status === "blocked" && !p.payload.reply),
    cause: posts[0] && state !== "running" && state !== "done" ? posts[0] : null,
    children,
  }
}

function state(agents: AgentNode[], connection: BoardState["connection"] = "live"): BoardState {
  const all: Post[] = []
  const walk = (a: AgentNode) => {
    all.push(...a.posts)
    a.children.forEach(walk)
  }
  agents.forEach(walk)
  all.sort((a, b) => b.updated_at.localeCompare(a.updated_at))
  return { posts: all, agents, connection, loaded: true }
}

export function sketchAgents(): AgentNode[] {
  return [
    agent(
      "backend",
      "Bill",
      "Backend",
      "ghost-bold",
      "attention",
      "error",
      [
        post("backend", "log", "Migration 0042 failed", "error", 4, false, {
          text: 'ERROR: relation "posts_v2" already exists',
        }),
        post("backend", "text", "Wired up post routes", "ok", 38),
      ],
      [
        agent("backend/db-integration", "Rachel", "DB integration", "user-circle-bold", "attention", "warn", [
          post("backend/db-integration", "table", "Slow queries over 200 ms", "warn", 6, false, {
            columns: ["query", "ms"],
            rows: [["select posts", 412]],
          }),
        ]),
      ],
    ),
    agent(
      "ui",
      "Tony",
      "UI",
      "mask-happy-bold",
      "needs-input",
      "blocked",
      [
        post("ui", "markdown", "Which layout for the detail view?", "blocked", 2, false, {
          text: "Option A: sheet. Option B: dialog.",
        }),
      ],
      [
        agent("ui/font-design", "Alex", "Font design", "smile-circle-bold", "needs-input", "blocked", [
          post("ui/font-design", "image", "Pick a type pairing", "blocked", 9),
        ]),
      ],
    ),
    agent("ux", "Mark", "UX", "user-rounded-bold", "running", "info", [
      post("ux", "log", "Running usability pass", "info", 0, true, { text: "checking focus order" }),
    ]),
    agent("research", "Thor", "Research", "emoji-funny-circle-bold", "attention", "error", [
      post("research", "text", "Source site returned 403", "error", 12),
    ]),
  ]
}

/** Stress case: several children, grandchildren, a done agent, a long role. */
export function deepAgents(): AgentNode[] {
  const [bill, tony, mark, thor] = sketchAgents()
  bill.children.push(
    agent(
      "backend/auth",
      "Priya",
      "Auth",
      "sticker-smile-circle-bold",
      "done",
      "ok",
      [post("backend/auth", "text", "Tokens rotate", "ok", 20)],
      [
        agent("backend/auth/sessions", "Omar", "Session storage", "ghost-smile-bold", "running", "info", [
          post("backend/auth/sessions", "log", "Benchmarking", "info", 1, true),
        ]),
      ],
    ),
  )
  tony.children.push(
    agent("ui/icons", "Jun", "Icon audit", "sticker-smile-square-bold", "done", "ok", [
      post("ui/icons", "text", "All icons Solar Bold", "ok", 15),
    ]),
  )
  return [
    bill,
    tony,
    mark,
    thor,
    agent("docs", "Maximiliana", "Documentation and release notes", "masks-bold", "done", "ok", [
      post("docs", "markdown", "README updated", "ok", 30),
    ]),
  ]
}

export function fixtureState(name: string | null): BoardState {
  switch (name) {
    case "empty":
      return state([])
    case "offline":
      return state(sketchAgents(), "offline")
    case "connecting":
      return state(sketchAgents(), "connecting")
    case "loading":
      return { posts: [], agents: [], connection: "connecting", loaded: false }
    case "deep":
      return state(deepAgents())
    default:
      return state(sketchAgents())
  }
}
