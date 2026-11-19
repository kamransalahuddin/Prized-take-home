// Regression tests: every "Needs input" comes with Approve/Reject for the post it is about, the
// home card and the detail page agree, and "Requires attention" points at the post behind it.
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it, vi } from "vitest"
import { AgentDetail } from "@/components/agent-detail"
import { agentState, buildAgents, isQuestion } from "./agents"
import type { AgentNode, Kind, Post } from "./types"

// Rendered with react-dom/server (no DOM here); the scrambling name effect needs `window`.
vi.mock("@/components/agent-name", () => ({ AgentName: ({ children }: { children: string }) => children }))

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
    gen: `g${n}`,
    payload: { text: `text ${n}` },
    ...extra,
  }
}

const PAYLOAD: Record<Kind, Record<string, unknown>> = {
  text: { text: "Can I?" },
  markdown: { markdown: "**Can I?**" },
  table: { columns: ["sku", "price"], rows: [["a", 10]] },
  chart: { option: { xAxis: {}, yAxis: {}, series: [] } },
  image: { asset_id: "a_1", url: "/api/assets/a_1" },
  log: { text: "plan: drop 3 tables\n", exit_code: null, truncated: false },
}

function find(tree: AgentNode[], id: string): AgentNode {
  for (const a of tree) {
    if (a.id === id) return a
    const hit = a.children.length ? findOrNull(a.children, id) : null
    if (hit) return hit
  }
  throw new Error("no agent " + id)
}
function findOrNull(tree: AgentNode[], id: string): AgentNode | null {
  try {
    return find(tree, id)
  } catch {
    return null
  }
}

/** Server-render the detail page: how many Approve buttons, and the whole markup. */
function detail(agent: AgentNode) {
  const html = renderToStaticMarkup(createElement(AgentDetail, { agent }))
  return {
    html,
    approve: (html.match(/class="[^"]*\bapprove\b/g) ?? []).length,
    label: /Needs input|Requires attention|Running|Done/.exec(html)?.[0],
  }
}

/** Home card label and the detail page label must be the same text. */
function cardLabel(agent: AgentNode) {
  return { "needs-input": "Needs input", attention: "Requires attention", running: "Running", done: "Done" }[agent.state]
}

function everyNode(tree: AgentNode[]): AgentNode[] {
  return tree.flatMap((a) => [a, ...everyNode(a.children)])
}

describe("isQuestion", () => {
  it("is any blocked post without a reply, of any kind", () => {
    for (const kind of Object.keys(PAYLOAD) as Kind[])
      expect(isQuestion(post("a", "blocked", 1, { kind, payload: PAYLOAD[kind] }))).toBe(true)
    expect(isQuestion(post("a", "blocked", 1, { payload: { text: "q", ask: true } }))).toBe(true)
    expect(isQuestion(post("a", "blocked", 1, { payload: { text: "q", reply: { action: "approve" } } }))).toBe(false)
    for (const s of ["info", "ok", "warn", "error"] as const) expect(isQuestion(post("a", s, 1))).toBe(false)
  })
})

describe("needs-you states always come with a way to respond", () => {
  it("warn/error (nobody waiting): Requires attention, no buttons, the warn/error post shown", () => {
    for (const status of ["warn", "error"] as const) {
      const bad = post("a", status, 2, { title: `Disk ${status}`, payload: { text: "disk almost full" } })
      const [a] = buildAgents([post("a", "info", 1), bad])
      expect(a.state).toBe("attention")
      expect(a.cause).toBe(bad)
      expect(a.questions).toEqual([])
      const d = detail(a)
      expect(d.label).toBe("Requires attention")
      expect(d.approve).toBe(0)
      expect(d.html).toContain("disk almost full")
    }
  })

  it("a blocked log post is a question: Needs input plus Approve/Reject", () => {
    const log = post("a", "blocked", 1, { kind: "log", title: "Apply this migration?", payload: PAYLOAD.log })
    const [a] = buildAgents([log])
    expect(a.state).toBe("needs-input")
    expect(a.questions).toEqual([log])
    const d = detail(a)
    expect(d.approve).toBe(1)
    expect(d.html).toContain("Apply this migration?")
    expect(d.html).toContain("plan: drop 3 tables")
  })

  it("a blocked post that already has a reply is not waiting: no Needs input without buttons", () => {
    const answered = post("a", "blocked", 1, { payload: { text: "q", reply: { action: "approve", text: null } } })
    expect(agentState([answered])).toEqual({ state: "running", status: "info", cause: null })
    const [a] = buildAgents([answered])
    expect(a.questions).toEqual([])
    const d = detail(a)
    expect(d.label).toBe("Running")
    expect(d.approve).toBe(0)
    expect(d.html).toContain("Approved")
  })

  it("an older blocked post with a newer ok post: card and page both Needs input, with buttons", () => {
    const q = post("a", "blocked", 1, { title: "Can I delete the users table?" })
    const [a] = buildAgents([q, post("a", "ok", 2, { title: "Migrations done" })])
    expect(a.state).toBe("needs-input")
    const d = detail(a)
    expect(d.label).toBe(cardLabel(a))
    expect(d.approve).toBe(1)
    expect(d.html).toContain("Migrations done") // the newer update still shows below
  })

  it("an older blocked post with newer info/warn posts keeps the buttons", () => {
    for (const s of ["info", "warn", "error"] as const) {
      const [a] = buildAgents([post("a", "blocked", 1), post("a", s, 2)])
      expect(a.state).toBe("needs-input")
      expect(detail(a).approve).toBe(1)
    }
  })

  it("a blocked post replaced via -k: the replacement decides (same id, new gen)", () => {
    const id = "p_keyed"
    const v1 = post("a", "blocked", 1, { id, key: "q", gen: "g1", title: "Deploy v1?" })
    // Re-asked blocked: still a question, answered against the new gen.
    const v2 = { ...v1, gen: "g2", title: "Deploy v2?", updated_at: "2026-11-18T10:02:00.000Z" }
    const [a] = buildAgents([v2])
    expect(a.questions.map((q) => q.gen)).toEqual(["g2"])
    expect(detail(a).approve).toBe(1)
    // Replaced with warn: Requires attention, no buttons, the replacement shown.
    const v3 = { ...v1, gen: "g3", status: "warn" as const, title: "Deploy postponed" }
    const [b] = buildAgents([v3])
    expect(b.state).toBe("attention")
    expect(b.cause).toBe(v3)
    const d = detail(b)
    expect(d.approve).toBe(0)
    expect(d.html).toContain("Deploy postponed")
  })

  it("board ask and post -s blocked both get Approve/Reject", () => {
    const asked = post("ask", "blocked", 1, { payload: { text: "Ship it?", ask: true } })
    const posted = post("posted", "blocked", 1, { payload: { text: "Ship it?" } })
    for (const a of buildAgents([asked, posted])) {
      expect(a.state).toBe("needs-input")
      expect(detail(a).approve).toBe(1)
    }
  })

  it("blocked image, table and chart posts show their content with the buttons, latest or not", () => {
    for (const kind of ["image", "table", "chart"] as const) {
      const q = post("a", "blocked", 1, { kind, title: `Review this ${kind}?`, payload: PAYLOAD[kind] })
      for (const posts of [[q], [q, post("a", "info", 2, { payload: { text: "Meanwhile writing tests" } })]]) {
        const [a] = buildAgents(posts)
        expect(a.questions).toEqual([q])
        const d = detail(a)
        expect(d.approve).toBe(1)
        expect(d.html).toContain(`Review this ${kind}?`)
        const marker = { image: "post-image", table: "<table", chart: 'class="chart' }[kind]
        expect(d.html).toContain(marker)
        if (posts.length > 1) expect(d.html).toContain("Meanwhile writing tests")
      }
    }
  })

  it("two open questions: both answerable", () => {
    const [a] = buildAgents([post("a", "blocked", 1, { title: "First?" }), post("a", "blocked", 2, { title: "Second?" })])
    expect(a.questions.map((q) => q.title)).toEqual(["Second?", "First?"])
    expect(detail(a).approve).toBe(2)
  })

  it("a parent that never posted, with a blocked subagent: its page shows the subagent's question", () => {
    const q = post("lead/ui", "blocked", 1, { title: "Pick a font", payload: { text: "Inter or Geist?" } })
    const tree = buildAgents([q])
    const lead = find(tree, "lead")
    expect(lead.state).toBe("needs-input")
    expect(lead.questions).toEqual([q])
    const d = detail(lead)
    expect(d.label).toBe("Needs input")
    expect(d.approve).toBe(1)
    expect(d.html).toContain("Inter or Geist?")
    expect(d.html).toContain('href="#agent/lead%2Fui"') // where it came from
  })

  it("a parent that never posted, with a warn subagent: its page shows the post behind the label", () => {
    const w = post("lead/tests", "warn", 1, { title: "Flaky test", payload: { text: "retrying login test" } })
    const lead = find(buildAgents([w]), "lead")
    expect(lead.state).toBe("attention")
    expect(lead.cause).toBe(w)
    const d = detail(lead)
    expect(d.label).toBe("Requires attention")
    expect(d.approve).toBe(0)
    expect(d.html).toContain("retrying login test")
  })

  it("a parent that posted keeps its own state; the blocked child is answerable on its own page", () => {
    const tree = buildAgents([post("lead", "info", 1), post("lead/ci", "blocked", 2, { title: "Merge PR 12?" })])
    const lead = find(tree, "lead")
    expect(lead.state).toBe("running")
    expect(lead.questions).toEqual([])
    expect(detail(lead).approve).toBe(0)
    expect(detail(find(tree, "lead/ci")).approve).toBe(1)
  })

  it("home card label == detail label, and Needs input <=> Approve/Reject, for every agent", () => {
    const tree = buildAgents([
      post("w", "warn", 1),
      post("e", "info", 1),
      post("e", "error", 2),
      post("log", "blocked", 1, { kind: "log", payload: PAYLOAD.log }),
      post("old", "blocked", 1),
      post("old", "ok", 2),
      post("ans", "blocked", 1, { payload: { text: "q", reply: { action: "reject", text: null } } }),
      post("p/c", "blocked", 1),
      post("pw/c", "error", 1),
      post("own", "info", 1),
      post("own/c", "blocked", 2, { kind: "chart", payload: PAYLOAD.chart }),
      post("img", "blocked", 1, { kind: "image", payload: PAYLOAD.image }),
      post("img", "info", 2),
      post("done", "ok", 1),
    ])
    for (const a of everyNode(tree)) {
      const d = detail(a)
      expect(d.label, a.id).toBe(cardLabel(a))
      expect(d.approve > 0, a.id).toBe(a.state === "needs-input")
      expect(d.approve, a.id).toBe(a.questions.length)
      if (a.state === "attention") expect(a.cause?.status, a.id).toMatch(/warn|error/)
    }
  })
})

describe("board ask --text: open questions get only a text box", () => {
  it("shows no Approve/Reject for a text-only question, but keeps the answer box", () => {
    const q = post("data", "blocked", 1, { payload: { text: "Drop or merge the duplicates?", ask: "text" } })
    const html = renderToStaticMarkup(createElement(AgentDetail, { agent: buildAgents([q])[0] }))
    expect(html).not.toContain("Approve")
    expect(html).not.toContain("Reject")
    expect(html).toContain('placeholder="Type your answer"')
  })

  it("a yes/no question still has Approve and Reject", () => {
    const q = post("data", "blocked", 1, { payload: { text: "Ship it?", ask: true } })
    const html = renderToStaticMarkup(createElement(AgentDetail, { agent: buildAgents([q])[0] }))
    expect(html).toContain("Approve")
    expect(html).toContain("Reject")
  })
})
