import { describe, expect, it } from "vitest"
import { applyEvent, applySnapshot, dropPending, emptyState, keepLastCodePoints, sortedPosts, type AppendData, type MergeState } from "./board"
import type { Post } from "./types"

function mk(id: string, seq: number, extra: Partial<Post> = {}): Post {
  const ts = `2026-11-18T10:00:${String(seq % 60).padStart(2, "0")}.000Z`
  return {
    id,
    agent: "lead",
    kind: "log",
    title: "pytest",
    status: "info",
    key: null,
    live: true,
    created_at: ts,
    updated_at: ts,
    seq,
    payload: { text: "a\n", truncated: false, exit_code: null },
    ...extra,
  }
}

function app(id: string, seq: number, text: string, extra: Partial<AppendData> = {}): AppendData {
  return {
    id,
    text,
    status: "info",
    title: "pytest",
    live: true,
    truncated: false,
    exit_code: null,
    updated_at: `2026-11-18T10:01:${String(seq % 60).padStart(2, "0")}.000Z`,
    seq,
    ...extra,
  }
}

function seeded(...posts: Post[]): MergeState {
  return applySnapshot(emptyState(), { posts, seq: Math.max(0, ...posts.map((p) => p.seq)) })
}

describe("applyEvent post", () => {
  it("adds a new post and tracks seq", () => {
    const { state, effect } = applyEvent(emptyState(), "post", mk("p1", 5))
    expect(effect).toBeNull()
    expect(state.posts.p1.seq).toBe(5)
    expect(state.seq).toBe(5)
  })

  it("replaces only when newer", () => {
    const s = seeded(mk("p1", 5, { title: "old" }))
    const newer = applyEvent(s, "post", mk("p1", 6, { title: "new" })).state
    expect(newer.posts.p1.title).toBe("new")
    const stale = applyEvent(newer, "post", mk("p1", 6, { title: "dup" }))
    expect(stale.state).toBe(newer)
    expect(applyEvent(newer, "post", mk("p1", 4)).state).toBe(newer)
  })
})

describe("applyEvent append", () => {
  it("concats log text and updates status/title/live/updated_at/seq/exit_code", () => {
    const s = seeded(mk("p1", 5))
    let r = applyEvent(s, "append", app("p1", 6, "b\n"))
    expect(r.effect).toBeNull()
    r = applyEvent(r.state, "append", app("p1", 7, "c\n", { status: "error", title: "pytest -x", live: false, exit_code: 1 }))
    const p = r.state.posts.p1
    expect(p.payload.text).toBe("a\nb\nc\n")
    expect(p.status).toBe("error")
    expect(p.title).toBe("pytest -x")
    expect(p.live).toBe(false)
    expect(p.payload.exit_code).toBe(1)
    expect(p.seq).toBe(7)
    expect(p.updated_at).toBe("2026-11-18T10:01:07.000Z")
    expect(r.state.seq).toBe(7)
    // input untouched
    expect(s.posts.p1.payload.text).toBe("a\n")
  })

  it("appends to text and markdown posts", () => {
    const s = seeded(
      mk("t", 1, { kind: "text", live: false, payload: { text: "hi" } }),
      mk("m", 2, { kind: "markdown", live: false, payload: { markdown: "# A" } }),
    )
    let st = applyEvent(s, "append", app("t", 3, "\nmore", { live: false })).state
    st = applyEvent(st, "append", app("m", 4, "\n\n- b", { live: false })).state
    expect(st.posts.t.payload.text).toBe("hi\nmore")
    expect(st.posts.m.payload.markdown).toBe("# A\n\n- b")
    expect(st.posts.m.payload.text).toBeUndefined()
  })

  it("ignores stale appends (seq <= post seq)", () => {
    const s = seeded(mk("p1", 5))
    expect(applyEvent(s, "append", app("p1", 5, "dup")).state).toBe(s)
    expect(applyEvent(s, "append", app("p1", 3, "old")).state).toBe(s)
  })

  it("unknown id: buffers, asks to fetch once, replays newer appends after the fetch", () => {
    let r = applyEvent(emptyState(), "append", app("px", 10, "x1\n"))
    expect(r.effect).toEqual({ type: "fetch", id: "px" })
    r = applyEvent(r.state, "append", app("px", 11, "x2\n"))
    expect(r.effect).toBeNull() // fetch already requested
    expect(r.state.posts.px).toBeUndefined()
    // fetched post already contains the seq 10 chunk, not seq 11
    const fetched = mk("px", 10, { payload: { text: "x0\nx1\n" } })
    const st = applyEvent(r.state, "post", fetched).state
    expect(st.posts.px.payload.text).toBe("x0\nx1\nx2\n")
    expect(st.posts.px.seq).toBe(11)
    expect(st.pending).toEqual({})
  })

  it("unknown id whose fetch fails is dropped", () => {
    const r = applyEvent(emptyState(), "append", app("px", 10, "x"))
    const st = dropPending(r.state, "px")
    expect(st.pending).toEqual({})
  })

  it("trims logs to the server cap when truncated", async () => {
    const { LOG_CAP } = await import("./board")
    const s = seeded(mk("p1", 1, { payload: { text: "x".repeat(LOG_CAP - 1) } }))
    const st = applyEvent(s, "append", app("p1", 2, "yy", { truncated: true })).state
    expect(st.posts.p1.payload.text.length).toBe(LOG_CAP)
    expect(st.posts.p1.payload.text.endsWith("yy")).toBe(true)
    expect(st.posts.p1.payload.truncated).toBe(true)
  })
})

describe("reset and snapshot", () => {
  it("reset asks for a refetch; snapshot drops untouched stale posts, keeps newer events", () => {
    const s = seeded(mk("old", 50), mk("both", 40))
    let r = applyEvent(s, "reset", { seq: 7 })
    expect(r.effect).toEqual({ type: "refetch" })
    expect(r.state.seq).toBe(7)
    // event after reset, before the snapshot arrives
    r = applyEvent(r.state, "post", mk("fresh", 8))
    const st = applySnapshot(r.state, { posts: [mk("both", 3, { title: "new db" })], seq: 7 })
    expect(Object.keys(st.posts).sort()).toEqual(["both", "fresh"])
    // "both" is replaced even though old local seq was higher: it came from another database
    expect(st.posts.both.title).toBe("new db")
    expect(st.touched).toBeNull()
    expect(st.seq).toBe(8)
  })

  it("normal snapshot merge keeps the higher seq copy", () => {
    let st = seeded(mk("a", 5))
    st = applyEvent(st, "append", app("a", 9, "z")).state
    st = applySnapshot(st, { posts: [mk("a", 6), mk("b", 7)], seq: 7 })
    expect(st.posts.a.seq).toBe(9)
    expect(st.posts.b.seq).toBe(7)
    expect(st.seq).toBe(9)
  })

  it("hello and unknown events change nothing", () => {
    const s = seeded(mk("a", 1))
    expect(applyEvent(s, "hello", { seq: 1 }).state).toBe(s)
    expect(applyEvent(s, "whatever", {}).state).toBe(s)
  })

  it("sortedPosts is newest updated_at first", () => {
    const st = seeded(
      mk("a", 1, { updated_at: "2026-11-18T10:00:01.000Z" }),
      mk("b", 2, { updated_at: "2026-11-18T10:00:03.000Z" }),
      mk("c", 3, { updated_at: "2026-11-18T10:00:02.000Z" }),
    )
    expect(sortedPosts(st).map((p) => p.id)).toEqual(["b", "c", "a"])
  })
})

describe("wiring against the hardened server", () => {
  it("a post fetched on its own never moves the stream cursor", () => {
    let s = applySnapshot(emptyState(), { posts: [mk("a", 3)], seq: 3, epoch: "e1" })
    // append for an unknown post at seq 4 -> buffered, fetch requested
    let r = applyEvent(s, "append", app("x", 4, "hi\n"))
    expect(r.effect).toEqual({ type: "fetch", id: "x" })
    s = r.state
    expect(s.seq).toBe(4)
    // the fetched copy is already at seq 9 (other events for it happened since)
    s = applyEvent(s, "fetched", mk("x", 9, { payload: { text: "full\n" } })).state
    expect(s.posts.x.seq).toBe(9)
    expect(s.seq).toBe(4) // events 5..8 for other posts must still be applied
    r = applyEvent(s, "append", app("a", 5, "b\n"))
    expect(r.state.posts.a.payload.text).toBe("a\nb\n")
  })

  it("a snapshot from another database (epoch) replaces everything", () => {
    let s = applySnapshot(emptyState(), { posts: [mk("old", 50)], seq: 50, epoch: "e1" })
    s = applySnapshot(s, { posts: [mk("new", 2)], seq: 2, epoch: "e2" })
    expect(Object.keys(s.posts)).toEqual(["new"])
    expect(s.seq).toBe(2)
    expect(s.epoch).toBe("e2")
  })

  it("hello with a different epoch asks for a fresh snapshot", () => {
    const s = applySnapshot(emptyState(), { posts: [mk("old", 5)], seq: 5, epoch: "e1" })
    const r = applyEvent(s, "hello", { seq: 1, epoch: "e2" })
    expect(r.effect).toEqual({ type: "refetch" })
    const st = applySnapshot(r.state, { posts: [mk("n", 1)], seq: 1, epoch: "e2" })
    expect(Object.keys(st.posts)).toEqual(["n"])
    // same epoch: nothing to do
    expect(applyEvent(s, "hello", { seq: 5, epoch: "e1" }).effect).toBeNull()
  })

  it("reset carries the epoch", () => {
    const s = applySnapshot(emptyState(), { posts: [mk("a", 5)], seq: 5, epoch: "e1" })
    const r = applyEvent(s, "reset", { seq: 1, epoch: "e9" })
    expect(r.state.epoch).toBe("e9")
    expect(r.effect).toEqual({ type: "refetch" })
  })

  it("status-only append (empty text) updates status without touching text", () => {
    const s = seeded(mk("a", 1))
    const p = applyEvent(s, "append", app("a", 2, "", { status: "error", live: false, exit_code: 3 })).state.posts.a
    expect(p.payload.text).toBe("a\n")
    expect(p.status).toBe("error")
    expect(p.live).toBe(false)
    expect(p.payload.exit_code).toBe(3)
  })

  it("log truncation keeps the last 2,000,000 code points, never splitting emoji", () => {
    expect(keepLastCodePoints("abc", 5)).toBe("abc")
    expect(keepLastCodePoints("abcdef", 3)).toBe("def")
    expect(keepLastCodePoints("a😀b😀", 3)).toBe("😀b😀")
    expect(keepLastCodePoints("😀😀😀", 2)).toBe("😀😀")
  })
})
