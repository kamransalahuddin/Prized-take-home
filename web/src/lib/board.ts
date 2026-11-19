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
  /** Stream cursor: highest seq received from the snapshot or the event stream. A post fetched
   * on its own (GET /api/posts/{id}) never moves it, or events for other posts would be skipped. */
  seq: number
  /** Database epoch (§5). A different epoch means another database: start over. */
  epoch: string | null
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

/** The server keeps exactly the last LOG_CAP code points of each log (store.LOG_CAP = 2 * 1024 *
 * 1024, §5); mirror that rule so a streamed log matches the snapshot. */
export const LOG_CAP = 2 * 1024 * 1024

/** Keep the last `cap` Unicode code points of `text` (never splits a surrogate pair). */
export function keepLastCodePoints(text: string, cap: number = LOG_CAP): string {
  if (text.length <= cap) return text // UTF-16 length >= code point count
  let i = text.length
  let n = 0
  while (i > 0 && n < cap) {
    const c = text.charCodeAt(i - 1)
    i -= c >= 0xdc00 && c <= 0xdfff && i >= 2 && (text.charCodeAt(i - 2) & 0xfc00) === 0xd800 ? 2 : 1
    n++
  }
  return text.slice(i)
}

export function emptyState(): MergeState {
  return { posts: {}, seq: 0, epoch: null, pending: {}, touched: null }
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
      if (post.kind === "log") text = keepLastCodePoints(text)
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
function putPost(state: MergeState, post: Post, fromStream = true): MergeState {
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
    ...state,
    posts: { ...state.posts, [post.id]: next },
    seq: fromStream ? Math.max(state.seq, post.seq) : state.seq,
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
    case "fetched": {
      // A post fetched on its own: merge it, but don't move the stream cursor.
      const post = data as Post
      if (!post || typeof post.id !== "string") return { state, effect: null }
      return { state: putPost(state, post, false), effect: null }
    }
    case "hello": {
      const epoch = (data as { epoch?: unknown })?.epoch
      if (typeof epoch === "string" && state.epoch !== null && epoch !== state.epoch) {
        // Another database is behind this server now: start over from its snapshot.
        return { state: { ...state, epoch, touched: [] }, effect: { type: "refetch" } }
      }
      return { state, effect: null }
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
      if (ev.seq <= cur.seq) return { state: ev.seq > state.seq ? { ...state, seq: ev.seq } : state, effect: null }
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
      const d = data as { seq?: unknown; epoch?: unknown }
      const seq = typeof d?.seq === "number" ? d.seq : state.seq
      const epoch = typeof d?.epoch === "string" ? d.epoch : state.epoch
      return { state: { ...state, seq, epoch, touched: [] }, effect: { type: "refetch" } }
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
export function applySnapshot(
  state: MergeState,
  snap: { posts: Post[]; seq: number; epoch?: string },
): MergeState {
  const posts: Record<string, Post> = {}
  const otherDb = snap.epoch !== undefined && state.epoch !== null && snap.epoch !== state.epoch
  if (otherDb) {
    // Nothing local belongs to this database (events touched since a reset came from it).
    if (state.touched) for (const id of state.touched) if (state.posts[id]) posts[id] = state.posts[id]
  } else if (state.touched === null) Object.assign(posts, state.posts)
  else for (const id of state.touched) if (state.posts[id]) posts[id] = state.posts[id]
  for (const p of snap.posts) {
    const cur = posts[p.id]
    if (!cur || p.seq > cur.seq) posts[p.id] = p
  }
  let next: MergeState = {
    posts,
    seq: otherDb ? snap.seq : Math.max(state.seq, snap.seq),
    epoch: snap.epoch ?? state.epoch,
    pending: otherDb ? {} : state.pending,
    touched: null,
  }
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
const EVENT_TYPES = ["hello", "post", "append", "reset", "ping"] as const
/** The server pings every 15 s; this long without any frame means a stalled connection. */
const WATCHDOG_MS = 45_000

function schedule(fn: () => void): () => void {
  if (typeof requestAnimationFrame === "function" && typeof document !== "undefined" && !document.hidden) {
    const h = requestAnimationFrame(fn)
    return () => cancelAnimationFrame(h)
  }
  // Hidden tabs don't run rAF; a timer keeps state fresh at a low rate.
  const h = setTimeout(fn, 250)
  return () => clearTimeout(h)
}

class HttpError extends Error {
  status: number
  constructor(url: string, status: number) {
    super(`${url}: HTTP ${status}`)
    this.status = status
  }
}

async function getJSON<T>(url: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, { signal, headers: { Accept: "application/json" } })
  if (!res.ok) throw new HttpError(url, res.status)
  return (await res.json()) as T
}

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms))
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
        // Retry through outages; only a 404 (post really gone) drops its buffered appends.
        for (let attempt = 0; !disposed; attempt++) {
          try {
            const post = await getJSON<Post>(`/api/posts/${encodeURIComponent(id)}`, abort.signal)
            if (!disposed) commit(applyEvent(stateRef.current, "fetched", post).state)
            return
          } catch (e) {
            if (disposed) return
            if (e instanceof HttpError && e.status === 404) {
              commit(dropPending(stateRef.current, id))
              return
            }
            if (!stateRef.current.pending[id]) return // a snapshot already brought it in
            await sleep(Math.min(1000 * 2 ** attempt, 10000))
          }
        }
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
            const snap = await getJSON<{ posts: Post[]; seq: number; epoch?: string }>(
              SNAPSHOT_URL,
              abort.signal,
            )
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

    let watchdog: ReturnType<typeof setTimeout> | null = null
    const feedWatchdog = () => {
      if (watchdog) clearTimeout(watchdog)
      watchdog = setTimeout(() => {
        watchdog = null
        // No frame (not even a ping) for 45 s: the connection is stalled (e.g. a proxy that
        // stopped forwarding). EventSource would wait forever, so reconnect ourselves.
        setConnection("offline")
        open()
      }, WATCHDOG_MS)
    }

    const handle = (type: string, raw: string) => {
      feedWatchdog()
      if (type === "ping") return
      if (type === "hello") setConnection("live")
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
      const { epoch, seq } = stateRef.current
      const src = new EventSource(`/api/events?since=${epoch ? `${epoch}:${seq}` : seq}`)
      es = src
      feedWatchdog()
      src.onopen = () => {
        setConnection("live")
        feedWatchdog()
      }
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
      if (watchdog) clearTimeout(watchdog)
    }
  }, [])

  const agents = useMemo(() => buildAgents(posts), [posts])
  return { posts, agents, connection, loaded }
}
