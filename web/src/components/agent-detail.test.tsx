import { describe, expect, it } from "vitest"
import { Fragment, isValidElement, type ReactElement, type ReactNode } from "react"
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

/** The direct children of <main> that AgentDetail renders (AgentDetail itself has no hooks),
 * with each open question's Fragment expanded so its decision section is visible too. */
function rendered(posts: Post[]) {
  const agent = buildAgents(posts)[0]
  const main = AgentDetail({ agent }) as ReactElement<{ children: ReactNode[] }>
  const top = main.props.children.flat().filter(isValidElement) as ReactElement[]
  const inner = top.flatMap((e) =>
    e.type === Fragment
      ? ([(e.props as { children: ReactNode[] }).children].flat(2).filter(isValidElement) as ReactElement[])
      : [e],
  )
  return { top, inner }
}
const keys = (els: ReactElement[]) => els.map((e) => e.key).filter((k) => k !== null)
const classOf = (e: ReactElement) => String((e.props as { className?: string }).className ?? "")

const table = { kind: "table" as const, title: "Drop these tables?", payload: { columns: ["t"], rows: [["users"]] } }

describe("AgentDetail decision vs update card", () => {
  it("never gives a question and an update card the same sibling key", () => {
    // With equal sibling keys React left an answered question's section orphaned on screen.
    for (const posts of [
      [post("p_q", 1, { ...table, status: "blocked" as const })],
      [post("p_old", 1), post("p_q", 2, { ...table, status: "blocked" as const })],
    ]) {
      const { top, inner } = rendered(posts)
      expect(inner.some((e) => classOf(e) === "decision")).toBe(true)
      const k = keys(top)
      expect(new Set(k).size).toBe(k.length)
    }
  })

  it("an answered question leaves the decision area and its title shows already revealed", () => {
    const { inner } = rendered([
      post("p_old", 1),
      post("p_q", 3, { ...table, payload: { ...table.payload, reply: { action: "approve", text: null, at: "x" } } }),
    ])
    expect(inner.some((e) => classOf(e) === "decision")).toBe(false)
    const card = inner.find((e) => classOf(e).includes("update"))
    expect(card?.key).toBe("p_q")
    const title = ([(card!.props as { children: ReactNode[] }).children].flat(2) as ReactElement[]).find(
      (c) => isValidElement(c) && (c.props as { revealed?: boolean }).revealed !== undefined,
    )
    expect((title!.props as { revealed: boolean }).revealed).toBe(true)
  })
})
