import { describe, expect, it } from "vitest"
import { AVATARS, NAMES, agentState, avatarFor, buildAgents, displayName, roleLabel } from "./agents"
import type { Post } from "./types"

let n = 0
function post(agent: string, status: Post["status"], minute: number, extra: Partial<Post> = {}): Post {
  n++
  const ts = `2026-11-18T10:${String(minute).padStart(2, "0")}:00.000Z`
  return {
    id: `p_${n}`,
    agent,
    kind: "text",
    title: null,
    status,
    key: null,
    live: false,
    created_at: ts,
    updated_at: ts,
    seq: n,
    payload: { text: "x" },
    ...extra,
  }
}

describe("buildAgents tree", () => {
  it("groups by agent and nests children, newest posts first", () => {
    const a = post("lead", "info", 1)
    const b = post("lead", "info", 3)
    const c = post("lead/tests", "info", 2)
    const tree = buildAgents([a, c, b])
    expect(tree).toHaveLength(1)
    expect(tree[0].id).toBe("lead")
    expect(tree[0].posts.map((p) => p.id)).toEqual([b.id, a.id])
    expect(tree[0].latest).toBe(b)
    expect(tree[0].children.map((x) => x.id)).toEqual(["lead/tests"])
    expect(tree[0].children[0].posts).toEqual([c])
    expect(tree[0].children[0].role).toBe("Tests")
  })

  it("creates a parent for an orphan child", () => {
    const c = post("ui/font-design", "blocked", 5)
    const tree = buildAgents([c])
    expect(tree).toHaveLength(1)
    const ui = tree[0]
    expect(ui.id).toBe("ui")
    expect(ui.posts).toEqual([])
    expect(ui.latest).toBeNull()
    expect(ui.role).toBe("UI")
    expect(ui.children[0].id).toBe("ui/font-design")
    expect(ui.children[0].state).toBe("needs-input")
    // parent summarizes its children when it has no posts
    expect(ui.state).toBe("needs-input")
    expect(ui.lastActivity).toBe(c.updated_at)
  })

  it("handles deeper paths", () => {
    const tree = buildAgents([post("a/b/c", "ok", 1)])
    expect(tree[0].id).toBe("a")
    expect(tree[0].children[0].id).toBe("a/b")
    expect(tree[0].children[0].children[0].id).toBe("a/b/c")
  })

  it("orders needs-input, then attention, then most recent activity", () => {
    const tree = buildAgents([
      post("old-done", "ok", 1),
      post("recent-running", "info", 9),
      post("warned", "warn", 2),
      post("stuck", "blocked", 0),
      post("failing", "error", 3),
      post("mid-running", "info", 5),
    ])
    expect(tree.map((t) => t.id)).toEqual([
      "stuck",
      "failing",
      "warned",
      "recent-running",
      "mid-running",
      "old-done",
    ])
  })

  it("orders children the same way and sorts a parent up when a child needs input", () => {
    const tree = buildAgents([
      post("a", "info", 9),
      post("b", "info", 1),
      post("b/x", "ok", 2),
      post("b/y", "blocked", 1),
      post("b/z", "error", 3),
    ])
    expect(tree.map((t) => t.id)).toEqual(["b", "a"])
    expect(tree[0].state).toBe("running") // own state is still its own posts
    expect(tree[0].children.map((c) => c.id)).toEqual(["b/y", "b/z", "b/x"])
  })

  it("live = any live log", () => {
    const tree = buildAgents([
      post("ux", "info", 1, { kind: "log", live: true, payload: { text: "" } }),
      post("ux", "ok", 2),
      post("qa", "info", 1, { live: true }), // live but not a log
    ])
    const ux = tree.find((t) => t.id === "ux")!
    const qa = tree.find((t) => t.id === "qa")!
    expect(ux.live).toBe(true)
    expect(qa.live).toBe(false)
  })
})

describe("agentState", () => {
  it("maps latest status", () => {
    expect(agentState([post("a", "info", 1)])).toEqual({ state: "running", status: "info" })
    expect(agentState([post("a", "ok", 1)])).toEqual({ state: "done", status: "ok" })
    expect(agentState([post("a", "warn", 1)])).toEqual({ state: "attention", status: "warn" })
    expect(agentState([post("a", "error", 1)])).toEqual({ state: "attention", status: "error" })
    expect(agentState([post("a", "ok", 1), post("a", "error", 2)]).state).toBe("attention")
  })

  it("blocked stays needs-input until a newer ok post", () => {
    const blocked = post("a", "blocked", 1)
    expect(agentState([blocked]).state).toBe("needs-input")
    expect(agentState([blocked, post("a", "info", 2)])).toEqual({ state: "needs-input", status: "blocked" })
    expect(agentState([blocked, post("a", "error", 2)]).state).toBe("needs-input")
    expect(agentState([blocked, post("a", "ok", 2)])).toEqual({ state: "done", status: "ok" })
    expect(agentState([blocked, post("a", "ok", 2), post("a", "info", 3)]).state).toBe("running")
    // an ok post older than the blocked one doesn't resolve it
    expect(agentState([post("a", "ok", 0), blocked]).state).toBe("needs-input")
  })

  it("a blocked post later marked ok (append -s ok) no longer blocks", () => {
    expect(agentState([post("a", "ok", 4), post("a", "info", 3)]).state).toBe("done")
  })
})

describe("identity", () => {
  it("displayName is deterministic and from the pool", () => {
    expect(NAMES.length).toBeGreaterThanOrEqual(55)
    expect(new Set(NAMES).size).toBe(NAMES.length)
    const ids = ["lead", "backend", "backend/db-integration", "ui", "ui/font-design", "ux", "research"]
    for (const id of ids) {
      expect(displayName(id)).toBe(displayName(id))
      expect(NAMES).toContain(displayName(id))
    }
    // varied: 7 ids shouldn't all collide
    expect(new Set(ids.map(displayName)).size).toBeGreaterThan(3)
    // full path matters
    const many = Array.from({ length: 50 }, (_, i) => displayName(`lead/sub${i}`))
    expect(new Set(many).size).toBeGreaterThan(20)
  })

  it("avatarFor is deterministic, and independent of name", () => {
    expect(avatarFor("ux")).toBe(avatarFor("ux"))
    expect(AVATARS).toContain(avatarFor("backend/db-integration"))
    const ids = Array.from({ length: 200 }, (_, i) => `agent-${i}`)
    // same name doesn't imply same avatar
    const pairs = new Set(ids.map((id) => `${displayName(id)}|${avatarFor(id)}`))
    const names = new Set(ids.map(displayName))
    expect(pairs.size).toBeGreaterThan(names.size)
    expect(new Set(ids.map(avatarFor)).size).toBe(AVATARS.length)
  })

  it("roleLabel humanizes the last segment", () => {
    expect(roleLabel("backend/db-integration")).toBe("DB integration")
    expect(roleLabel("ui/font-design")).toBe("Font design")
    expect(roleLabel("lead")).toBe("Lead")
    expect(roleLabel("ux")).toBe("UX")
    expect(roleLabel("lead/api_client.v2")).toBe("API client v2")
    expect(roleLabel("lead/fix-CI-flakes")).toBe("Fix CI flakes")
    expect(roleLabel("lead/aws-gpu-ml")).toBe("AWS GPU ML")
  })
})
