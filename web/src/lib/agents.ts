import people from "./people.json"
// Pure helpers that turn a flat list of posts into the agent tree the home page shows.
import type { AgentNode, AgentState, Post, Status } from "./types"

// ------------------------------------------------------------------ identity

/** 32-bit FNV-1a over the UTF-16 code units of `s`. Stable across runs and platforms. */
export function fnv1a(s: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return h >>> 0
}

export const NAMES = [
  "Alex", "Avery", "Bailey", "Blair", "Lake", "Cameron", "Casey", "Charlie",
  "Dakota", "Devon", "Drew", "Eden", "Ari", "Emery", "Finley", "Frankie",
  "Gray", "Harper", "Hayden", "Indigo", "Jamie", "Ash", "Jordan", "Jules",
  "Kai", "Kendall", "Kit", "Lane", "Logan", "Lou", "Marley", "Aster",
  "Morgan", "Echo", "Winter", "Oakley", "Parker", "Peyton", "Quinn", "Reese",
  "Remy", "Riley", "River", "Robin", "Rowan", "Sage", "Sam", "Aspen",
  "Shay", "Sky", "Cedar", "Stevie", "Linden", "Tatum", "Taylor", "Storm",
  "Val", "Wren", "Rain", "Shiloh",
] as const

export const AVATARS = Object.keys(people)

/** Stable first name for an agent path ("backend/db-integration" → e.g. "Riley"). */
export function displayName(id: string): string {
  return NAMES[fnv1a(id) % NAMES.length]
}

/** Stable UIcons portrait name for an agent path; salted so it varies independently of the name. */
export function avatarFor(id: string): string {
  const hash = fnv1a(`avatar:${id}`)
  const mixed = Math.imul(hash ^ (hash >>> 16), 0x85ebca6b)
  return AVATARS[((mixed ^ (mixed >>> 13)) >>> 0) % AVATARS.length]
}

const ACRONYMS = new Set([
  "db", "ui", "ux", "api", "ci", "qa", "sql", "css", "html", "js", "ts",
  "io", "ml", "ai", "cli", "sdk", "aws", "gpu",
])

/** Humanized last path segment: "db-integration" → "DB integration", "font_design" → "Font design". */
export function roleLabel(id: string): string {
  const last = id.split("/").filter(Boolean).pop() ?? id
  const words = last.split(/[-_.\s]+/).filter(Boolean).map((w) => w.toLowerCase())
  if (words.length === 0) return last
  return words
    .map((w, i) => {
      if (ACRONYMS.has(w)) return w.toUpperCase()
      return i === 0 ? w.charAt(0).toUpperCase() + w.slice(1) : w
    })
    .join(" ")
}

// ------------------------------------------------------------------ state

/** Newest first: updated_at desc, then seq desc (ISO strings from the server sort lexically). */
export function compareNewest(a: Post, b: Post): number {
  if (a.updated_at !== b.updated_at) return a.updated_at < b.updated_at ? 1 : -1
  return b.seq - a.seq
}

/** How urgently a state needs the human; lower = more urgent. */
export const STATE_RANK: Record<AgentState, number> = {
  "needs-input": 0,
  attention: 1,
  running: 2,
  done: 3,
}

/**
 * A post the human can still answer: `blocked` with no reply yet, of any kind (text, Markdown,
 * table, chart, image or log; from `board ask` or `-s blocked`). The server takes a reply to
 * exactly these (`Store.reply`) and `board wait` keeps waiting on them. A newer post from the
 * agent does not close it; a reply, `board append ID -s ...` or a `-k` replace does.
 */
export function isQuestion(p: Post): boolean {
  return p.status === "blocked" && !p.payload?.reply
}

/**
 * State of one agent from its own posts (any order), and the post behind it.
 * - any open question (`isQuestion`) → needs-input; `cause` = the newest question
 * - else the latest post's status: error/warn → attention (`cause` = that post), ok → done,
 *   info → running
 */
export function agentState(posts: Post[]): { state: AgentState; status: Status; cause: Post | null } {
  if (posts.length === 0) return { state: "running", status: "info", cause: null }
  const sorted = [...posts].sort(compareNewest)
  const question = sorted.find(isQuestion)
  if (question) return { state: "needs-input", status: "blocked", cause: question }
  const latest = sorted[0]
  switch (latest.status) {
    case "error":
    case "warn":
      return { state: "attention", status: latest.status, cause: latest }
    case "ok":
      return { state: "done", status: "ok", cause: null }
    default:
      // a "blocked" post here already has a reply (it can't be answered again): treat as info
      return { state: "running", status: "info", cause: null }
  }
}

// ------------------------------------------------------------------ tree

interface Draft {
  id: string
  posts: Post[]
  children: Map<string, Draft>
}

/** Most urgent state in a subtree (used for ordering, so a parent with a blocked child sorts up). */
function subtreeRank(n: AgentNode): number {
  let r = STATE_RANK[n.state]
  for (const c of n.children) r = Math.min(r, subtreeRank(c))
  return r
}

function sortNodes(nodes: AgentNode[]): AgentNode[] {
  const ranked = nodes.map((n) => ({ n, r: subtreeRank(n) }))
  ranked.sort((a, b) => {
    if (a.r !== b.r) return a.r - b.r
    if (a.n.lastActivity !== b.n.lastActivity) return a.n.lastActivity < b.n.lastActivity ? 1 : -1
    return a.n.id < b.n.id ? -1 : a.n.id > b.n.id ? 1 : 0
  })
  return ranked.map((x) => x.n)
}

function finish(d: Draft): AgentNode {
  const children = sortNodes([...d.children.values()].map(finish))
  const posts = [...d.posts].sort(compareNewest)
  let state: AgentState
  let status: Status
  let cause: Post | null
  let questions: Post[]
  let live: boolean
  if (posts.length > 0) {
    ;({ state, status, cause } = agentState(posts))
    questions = posts.filter(isQuestion)
    live = posts.some((p) => p.kind === "log" && p.live)
  } else {
    // A parent that never posted itself: summarize its children so the card isn't blank, and
    // carry their open questions and the post behind the state so its page can show them.
    const top = [...children].sort((a, b) => STATE_RANK[a.state] - STATE_RANK[b.state])[0]
    state = top?.state ?? "running"
    status = top?.status ?? "info"
    cause = top?.cause ?? null
    questions = children.flatMap((c) => c.questions).sort(compareNewest)
    live = children.some((c) => c.live)
  }
  // Newest change in this agent or anything under it.
  let lastActivity = posts[0]?.updated_at ?? ""
  for (const c of children) if (c.lastActivity > lastActivity) lastActivity = c.lastActivity
  return {
    id: d.id,
    name: displayName(d.id),
    role: roleLabel(d.id),
    avatar: avatarFor(d.id),
    state,
    status,
    live,
    lastActivity,
    latest: posts[0] ?? null,
    posts,
    questions,
    cause,
    children,
  }
}

/**
 * Group posts by agent path and nest subagents under their parents ("ui/font-design" under
 * "ui"), creating parents that never posted. Ordered needs-input, then attention, then by most
 * recent activity (at every level).
 */
export function buildAgents(posts: Post[]): AgentNode[] {
  const roots = new Map<string, Draft>()
  const byId = new Map<string, Draft>()
  const ensure = (path: string): Draft => {
    let d = byId.get(path)
    if (d) return d
    d = { id: path, posts: [], children: new Map() }
    byId.set(path, d)
    const cut = path.lastIndexOf("/")
    if (cut > 0) ensure(path.slice(0, cut)).children.set(path, d)
    else roots.set(path, d)
    return d
  }
  for (const p of posts) {
    const path = p.agent.split("/").filter(Boolean).join("/") || p.agent
    ensure(path).posts.push(p)
  }
  return sortNodes([...roots.values()].map(finish))
}
