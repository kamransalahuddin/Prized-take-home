// Data layer: snapshot + SSE merge (contract: docs/contract.md §3–5).
// The merge logic is pure (applyEvent / applySnapshot) so it can be tested without a browser;
// useBoard() wires it to fetch + EventSource and batches renders to one per animation frame.
import { useEffect, useMemo, useRef, useState } from "react"
import { buildAgents, compareNewest } from "./agents"
import type { BoardState, Connection, Post, Status } from "./types"

/** `append` event payload (§5). `text` is the exact suffix to concatenate. */
export interface AppendData {
  id: string
  agent?: string
  kind?: string
  text: string
  status: Status
  title: string | null
  live: boolean
  truncated?: boolean
  exit_code?: number | null
  updated_at: string
  seq: number
}

export interface MergeState {
  posts: Record<string, Post>
  /** Highest event seq applied. */
  seq: number
  /** Appends for posts we don't have yet, waiting for GET /api/posts/{id}. */
  pending: Record<string, AppendData[]>
  /** Ids touched since a `reset`, until the fresh snapshot lands (null = no reset in progress). */
  touched: string[] | null
}

/** Side effect the caller should perform after applying an event. */
export type Effect = { type: "fetch"; id: string } | { type: "refetch" } | null

export interface ApplyResult {
  state: MergeState
  effect: Effect
}

/** Server keeps the last 2 MB of each log (store.LOG_CAP); mirror that so memory stays bounded. */
export const LOG_CAP = 2 * 1024 * 1024

export function emptyState(): MergeState {
  return { posts: {}, seq: 0, pending: {}, touched: null }
}

function touch(state: MergeState, id: string): string[] | null {
  if (state.touched === null || state.touched.includes(id)) return state.touched
  return [...state.touched, id]
}

function appendInto(post: Post, ev: AppendData): Post {
  const payload = { ...post.payload }
  if (ev.text) {
    if (post.kind === "markdown") {
      payload.markdown = String(payload.markdown ?? "") + ev.text
    } else {
      let text = String(payload.text ?? "") + ev.text
      if (post.kind === "log" && text.length > LOG_CAP) text = text.slice(text.length - LOG_CAP)
      payload.text = text
    }
  }
  if (post.kind === "log") {
    if (ev.truncated !== undefined) payload.truncated = ev.truncated
    if (ev.exit_code !== undefined) payload.exit_code = ev.exit_code
  }
  return {
    ...post,
    payload,
    status: ev.status,
    title: ev.title,
    live: ev.live,
    updated_at: ev.updated_at,
    seq: ev.seq,
  }
}

/** Put a full post into state if it's newer than what we have, then replay any waiting appends. */
function putPost(state: MergeState, post: Post): MergeState {
  const cur = state.posts[post.id]
  const waiting = state.pending[post.id]
  if (cur && post.seq <= cur.seq && !waiting) return state
  let next = cur && post.seq <= cur.seq ? cur : post
  let pending = state.pending
  if (waiting) {
    for (const ev of waiting) if (ev.seq > next.seq) next = appendInto(next, ev)
    pending = { ...state.pending }
    delete pending[post.id]
  }
  return {
    posts: { ...state.posts, [post.id]: next },
    seq: Math.max(state.seq, post.seq),
    pending,
    touched: touch(state, post.id),
  }
}

/**
 * Apply one SSE event (or a fetched post, as type "post") to the state. Pure: returns the same
 * state object when nothing changed (stale seq, hello, unknown type).
 * - post: replace if data.seq > local seq
 * - append: concat text onto payload.text (log/text) or payload.markdown, update status/title/
 *   live/updated_at/seq; unknown id → buffer it and ask the caller to fetch the post
 * - reset: ask the caller to refetch the snapshot
 */
export function applyEvent(state: MergeState, type: string, data: unknown): ApplyResult {
  switch (type) {
    case "post": {
      const post = data as Post
      if (!post || typeof post.id !== "string") return { state, effect: null }
      return { state: putPost(state, post), effect: null }
    }
    case "append": {
      const ev = data as AppendData
      if (!ev || typeof ev.id !== "string") return { state, effect: null }
      const cur = state.posts[ev.id]
      if (!cur) {
        const waiting = state.pending[ev.id]
        if (waiting?.some((w) => w.seq >= ev.seq)) return { state, effect: null }
        return {
          state: {
            ...state,
            seq: Math.max(state.seq, ev.seq),
            pending: { ...state.pending, [ev.id]: [...(waiting ?? []), ev] },
          },
          effect: waiting ? null : { type: "fetch", id: ev.id },
        }
      }
      if (ev.seq <= cur.seq) return { state, effect: null }
      return {
        state: {
          ...state,
          posts: { ...state.posts, [ev.id]: appendInto(cur, ev) },
          seq: Math.max(state.seq, ev.seq),
          touched: touch(state, ev.id),
        },
        effect: null,
      }
    }
    case "reset": {
      // The stream continues from the server's current seq; restart our counter there.
      const seq = typeof (data as { seq?: unknown })?.seq === "number" ? (data as { seq: number }).seq : state.seq
      return { state: { ...state, seq, touched: [] }, effect: { type: "refetch" } }
    }
    default:
      return { state, effect: null }
  }
}

/**
 * Merge a GET /api/posts snapshot. Posts present locally keep whichever copy has the higher seq.
 * After a `reset`, local posts are dropped unless the snapshot has them or an event touched them
 * since the reset (they may come from a different database).
 */
export function applySnapshot(state: MergeState, snap: { posts: Post[]; seq: number }): MergeState {
  const posts: Record<string, Post> = {}
  if (state.touched === null) Object.assign(posts, state.posts)
  else for (const id of state.touched) if (state.posts[id]) posts[id] = state.posts[id]
  for (const p of snap.posts) {
    const cur = posts[p.id]
    if (!cur || p.seq > cur.seq) posts[p.id] = p
  }
  let next: MergeState = { posts, seq: Math.max(state.seq, snap.seq), pending: state.pending, touched: null }
  // Appends that were waiting for a post the snapshot now includes.
  for (const id of Object.keys(state.pending)) if (posts[id]) next = putPost(next, posts[id])
  return next
}

/** Posts newest `updated_at` first. */
export function sortedPosts(state: MergeState): Post[] {
  return Object.values(state.posts).sort(compareNewest)
}

/** Drop a pending fetch that failed (post gone): forget its buffered appends. */
export function dropPending(state: MergeState, id: string): MergeState {
  if (!state.pending[id]) return state
  const pending = { ...state.pending }
  delete pending[id]
  return { ...state, pending }
}

// ------------------------------------------------------------------ hook

const SNAPSHOT_URL = "/api/posts?limit=2000"
const EVENT_TYPES = ["hello", "post", "append", "reset"] as const

function schedule(fn: () => void): () => void {
  if (typeof requestAnimationFrame === "function" && typeof document !== "undefined" && !document.hidden) {
    const h = requestAnimationFrame(fn)
    return () => cancelAnimationFrame(h)
  }
  // Hidden tabs don't run rAF; a timer keeps state fresh at a low rate.
  const h = setTimeout(fn, 250)
  return () => clearTimeout(h)
}

async function getJSON<T>(url: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, { signal, headers: { Accept: "application/json" } })
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`)
  return (await res.json()) as T
}

export function useBoard(): BoardState {
  const [posts, setPosts] = useState<Post[]>([])
  const [connection, setConnection] = useState<Connection>("connecting")
  const [loaded, setLoaded] = useState(false)
  const stateRef = useRef<MergeState>(emptyState())

  useEffect(() => {
    let disposed = false
    let es: EventSource | null = null
    let cancelFlush: (() => void) | null = null
    let reopenTimer: ReturnType<typeof setTimeout> | null = null
    let rendered: Record<string, Post> | null = null
    const inflight = new Set<string>()
    const abort = new AbortController()

    const flush = () => {
      cancelFlush = null
      if (disposed) return
      const s = stateRef.current
      if (s.posts === rendered) return
      rendered = s.posts
      setPosts(sortedPosts(s))
    }
    const requestFlush = () => {
      if (!cancelFlush) cancelFlush = schedule(flush)
    }
    const commit = (next: MergeState) => {
      if (next === stateRef.current) return
      stateRef.current = next
      requestFlush()
    }

    const fetchPost = async (id: string) => {
      if (inflight.has(id)) return
      inflight.add(id)
      try {
        const post = await getJSON<Post>(`/api/posts/${encodeURIComponent(id)}`, abort.signal)
        if (!disposed) commit(applyEvent(stateRef.current, "post", post).state)
      } catch {
        if (!disposed) commit(dropPending(stateRef.current, id))
      } finally {
        inflight.delete(id)
      }
    }

    let loading: Promise<void> | null = null
    const loadSnapshot = (): Promise<void> => {
      if (loading) return loading
      loading = (async () => {
        for (let attempt = 0; !disposed; attempt++) {
          try {
            const snap = await getJSON<{ posts: Post[]; seq: number }>(SNAPSHOT_URL, abort.signal)
            if (disposed) return
            commit(applySnapshot(stateRef.current, snap))
            cancelFlush?.()
            flush()
            setLoaded(true)
            return
          } catch {
            if (disposed) return
            setConnection("offline")
            await new Promise((r) => setTimeout(r, Math.min(1000 * 2 ** attempt, 10000)))
          }
        }
      })().finally(() => {
        loading = null
      })
      return loading
    }

    const handle = (type: string, raw: string) => {
      if (type === "hello") {
        setConnection("live")
        return
      }
      let data: unknown
      try {
        data = JSON.parse(raw)
      } catch {
        return
      }
      const { state, effect } = applyEvent(stateRef.current, type, data)
      commit(state)
      if (effect?.type === "fetch") void fetchPost(effect.id)
      else if (effect?.type === "refetch") void loadSnapshot()
    }

    const open = () => {
      if (disposed) return
      es?.close()
      const src = new EventSource(`/api/events?since=${stateRef.current.seq}`)
      es = src
      src.onopen = () => setConnection("live")
      src.onerror = () => {
        setConnection("offline")
        // CONNECTING = the browser retries by itself (with Last-Event-ID). CLOSED = it gave up
        // (e.g. HTTP error), so reopen ourselves from the last seq we applied.
        if (src.readyState === EventSource.CLOSED && !reopenTimer) {
          reopenTimer = setTimeout(() => {
            reopenTimer = null
            open()
          }, 2000)
        }
      }
      for (const t of EVENT_TYPES) {
        src.addEventListener(t, (e) => handle(t, (e as MessageEvent<string>).data))
      }
    }

    void loadSnapshot().then(open)

    return () => {
      disposed = true
      abort.abort()
      es?.close()
      cancelFlush?.()
      if (reopenTimer) clearTimeout(reopenTimer)
    }
  }, [])

  const agents = useMemo(() => buildAgents(posts), [posts])
  return { posts, agents, connection, loaded }
}
