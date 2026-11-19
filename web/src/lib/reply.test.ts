import { describe, expect, it, vi } from "vitest"
import { applyEvent, applySnapshot, emptyState, sortedPosts } from "./board"
import { buildAgents } from "./agents"
import { nextAttempt, replyBody, sendReply } from "./reply"
import type { Post } from "./types"

function question(extra: Partial<Post> = {}): Post {
  return {
    id: "p_q",
    agent: "lead",
    kind: "text",
    title: null,
    status: "blocked",
    key: null,
    live: false,
    created_at: "2026-11-18T10:00:00.000Z",
    updated_at: "2026-11-18T10:00:00.000Z",
    seq: 1,
    gen: "g1",
    payload: { text: "Can I delete the users table?", ask: true },
    ...extra,
  }
}
const answered = (action = "approve", text: string | null = null): Post =>
  question({
    status: "info",
    seq: 2,
    updated_at: "2026-11-18T10:00:05.000Z",
    payload: { text: "Can I delete the users table?", ask: true, reply: { action, text, at: "2026-11-18T10:00:05.000Z" } },
  })

function respond(status: number, body: unknown) {
  return vi.fn(async () => new Response(typeof body === "string" ? body : JSON.stringify(body), { status }))
}

describe("reply request", () => {
  it("sends action, trimmed text, expect_gen and the attempt's request_id", async () => {
    const f = respond(200, answered("answer", "soft delete"))
    const body = replyBody(question(), "answer", "  soft delete ")
    const res = await sendReply(question(), body, { key: "k", id: "rid-1" }, f as unknown as typeof fetch)
    expect(res).toEqual({ ok: true, post: answered("answer", "soft delete") })
    const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe("/api/posts/p_q/reply")
    expect(JSON.parse(String(init.body))).toEqual({
      action: "answer",
      text: "soft delete",
      expect_gen: "g1",
      request_id: "rid-1",
    })
  })

  it("reuses the request id only for an identical retry", () => {
    let n = 0
    const id = () => "id" + ++n
    const approve = replyBody(question(), "approve", "")
    const a = nextAttempt(null, approve, id)
    expect(nextAttempt(a, replyBody(question(), "approve", ""), id)).toBe(a)
    expect(nextAttempt(a, replyBody(question(), "reject", ""), id).id).toBe("id2")
    // A re-asked question (new gen) never reuses the old id.
    expect(nextAttempt(a, replyBody(question({ gen: "g2" }), "approve", ""), id).id).toBe("id3")
  })

  it("returns the server's error for a stale question (409) and other failures", async () => {
    const stale = "the question was replaced by a newer one; reload and answer that"
    const body = replyBody(question(), "approve", "")
    const at = { key: "k", id: "r" }
    expect(await sendReply(question(), body, at, respond(409, { error: stale }) as unknown as typeof fetch)).toEqual({
      ok: false,
      error: stale,
    })
    expect(await sendReply(question(), body, at, respond(500, "oops") as unknown as typeof fetch)).toEqual({
      ok: false,
      error: "Could not send reply",
    })
    const down = vi.fn(async () => {
      throw new TypeError("Failed to fetch")
    })
    expect(await sendReply(question(), body, at, down as unknown as typeof fetch)).toEqual({
      ok: false,
      error: "Failed to fetch",
    })
  })

  it("a 200 without a readable post is still a success, with nothing to merge", async () => {
    const body = replyBody(question(), "approve", "")
    expect(await sendReply(question(), body, { key: "k", id: "r" }, respond(200, "") as unknown as typeof fetch)).toEqual({
      ok: true,
      post: null,
    })
  })
})

describe("merging the reply response (no interim state)", () => {
  it("one merge turns the question into the answered post; the later SSE event is a no-op", () => {
    const start = applySnapshot(emptyState(), { posts: [question()], seq: 1 })
    expect(buildAgents(sortedPosts(start))[0].state).toBe("needs-input")

    // The page merges the reply response as a fetched post: answered at once, cursor unchanged.
    const merged = applyEvent(start, "fetched", answered()).state
    expect(merged.seq).toBe(1)
    const agent = buildAgents(sortedPosts(merged))[0]
    expect(agent.state).toBe("running")
    expect(agent.posts[0].payload.reply.action).toBe("approve")
    expect(agent.posts.some((p) => p.status === "blocked" && !p.payload.reply)).toBe(false)

    // The SSE `post` event for the same write arrives afterwards: nothing re-renders.
    expect(applyEvent(merged, "post", answered()).state.posts).toBe(merged.posts)
  })

  it("a reply response older than what the stream already delivered never rolls it back", () => {
    const reasked = question({ seq: 3, gen: "g2", payload: { text: "Second question?", ask: true } })
    const s = applySnapshot(emptyState(), { posts: [reasked], seq: 3 })
    const merged = applyEvent(s, "fetched", answered()).state
    expect(merged.posts["p_q"]).toBe(reasked)
  })
})
