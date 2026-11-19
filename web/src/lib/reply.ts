// Answering a blocked post (contract §4: POST /api/posts/{id}/reply). Kept free of React so the
// request/idempotency/error rules can be tested without a browser.
import type { Post } from "./types"

export type ReplyAction = "approve" | "reject" | "answer"

/** One request id per distinct reply: retrying the same click after a network error reuses it,
 * so the server applies the answer at most once (§4 request_id). */
export interface ReplyAttempt {
  key: string
  id: string
}

/** `post` is the answered post from the response, or null if the body could not be read (the
 * reply was still applied). */
export type ReplyResult = { ok: true; post: Post | null } | { ok: false; error: string }

function newId() {
  return crypto.randomUUID?.() ?? String(Math.random()).slice(2) + Date.now()
}

export function replyBody(post: Pick<Post, "gen">, action: ReplyAction, text: string) {
  return { action, text: text.trim() || null, expect_gen: post.gen }
}

/** Reuse the previous attempt's request id when the body is identical, else start a new one. */
export function nextAttempt(
  prev: ReplyAttempt | null,
  body: ReturnType<typeof replyBody>,
  makeId: () => string = newId,
): ReplyAttempt {
  const key = JSON.stringify(body)
  return prev?.key === key ? prev : { key, id: makeId() }
}

/**
 * Send the reply. On success the server returns the full answered post (`payload.reply` set,
 * status `info`); the caller merges it into the board state at once, so the question turns into
 * its answered card in a single render instead of showing an interim "sent" message until the
 * matching SSE `post` event arrives.
 */
export async function sendReply(
  post: Pick<Post, "id">,
  body: ReturnType<typeof replyBody>,
  attempt: ReplyAttempt,
  fetchFn: typeof fetch = fetch,
): Promise<ReplyResult> {
  try {
    const response = await fetchFn("/api/posts/" + encodeURIComponent(post.id) + "/reply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, request_id: attempt.id }),
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) return { ok: false, error: (data && data.error) || "Could not send reply" }
    const answered = data && typeof data.id === "string" && data.payload?.reply ? (data as Post) : null
    return { ok: true, post: answered }
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "Could not send reply" }
  }
}
