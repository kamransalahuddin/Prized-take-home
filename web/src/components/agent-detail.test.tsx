import { describe, expect, it } from "vitest"
import { isValidElement, type ReactElement, type ReactNode } from "react"
import { AgentDetail } from "./agent-detail"
import { buildAgents } from "@/lib/agents"
import type { Post } from "@/lib/types"

function post(id: string, seq: number, extra: Partial<Post> = {}): Post {
  const ts = `2026-11-18T10:00:${String(seq).padStart(2, "0")}.000Z`
  return {
    id,
    agent: "lead",
    kind: "text",
    title: null,
    status: "info",
    key: null,
    live: false,
    created_at: ts,
    updated_at: ts,
    seq,
    gen: "g" + seq,
    payload: { text: "post " + id },
    ...extra,
  }
}

/** The direct children of <main> that AgentDetail renders (AgentDetail itself has no hooks). */
function sections(posts: Post[]): ReactElement[] {
  const agent = buildAgents(posts)[0]
  const main = AgentDetail({ agent }) as ReactElement<{ children: ReactNode[] }>
  return main.props.children.flat().filter(isValidElement) as ReactElement[]
}
const keys = (els: ReactElement[]) => els.map((e) => e.key).filter((k) => k !== null)
const classOf = (e: ReactElement) => String((e.props as { className?: string }).className ?? "")

const table = { kind: "table" as const, title: "Drop these tables?", payload: { columns: ["t"], rows: [["users"]] } }

describe("AgentDetail decision vs update card", () => {
  it("never gives the question section and the update card the same key", () => {
    // A non-message question that is also the latest post fills both slots with one post; with
    // equal sibling keys React left the answered question's section orphaned on screen.
    const els = sections([post("p_old", 1), post("p_q", 2, { ...table, status: "blocked" })])
    expect(els.some((e) => classOf(e) === "decision")).toBe(true)
    expect(els.some((e) => classOf(e).includes("update"))).toBe(true)
    const k = keys(els)
    expect(new Set(k).size).toBe(k.length)
  })

  it("the update card keeps its key when the question is answered (no remount)", () => {
    const before = sections([post("p_old", 1), post("p_q", 2, { ...table, status: "blocked" })])
    const after = sections([
      post("p_old", 1),
      post("p_q", 3, { ...table, payload: { ...table.payload, reply: { action: "approve", text: null, at: "x" } } }),
    ])
    const card = (els: ReactElement[]) => els.find((e) => classOf(e).includes("update"))
    expect(after.some((e) => classOf(e) === "decision")).toBe(false)
    expect(card(after)?.key).toBe(card(before)?.key)
  })
})
