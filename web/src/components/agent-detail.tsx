import { AgentName } from "./agent-name"
import { useEffect, useRef, useState } from "react"
import { TextAnimate } from "./magicui/text-animate"
import { InteractiveHoverButton } from "./magicui/interactive-hover-button"
import { TweetCard } from "./magicui/tweet-card"
import { Icon } from "./icon"
import { StatusLabel } from "./status-label"
import { PostContent } from "./post-content"
import type { AgentNode, Post } from "@/lib/types"
import { applyLocalPost } from "@/lib/board"
import { nextAttempt, replyBody, sendReply, type ReplyAction, type ReplyAttempt } from "@/lib/reply"

export function postTitle(post: Post) {
  return (
    post.title ||
    String(post.payload.text || post.payload.markdown || post.kind)
      .split("\n")[0]
      .slice(0, 160) ||
    post.kind
  )
}
export function Time({ value }: { value: string }) {
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60000)
    return () => clearInterval(timer)
  }, [])
  const mins = Math.max(0, Math.floor((now - Date.parse(value)) / 60000))
  return (
    <time dateTime={value} title={new Date(value).toLocaleString()}>
      {mins < 1
        ? "Just now"
        : mins < 60
          ? mins + "m ago"
          : mins < 1440
            ? Math.floor(mins / 60) + "h ago"
            : Math.floor(mins / 1440) + "d ago"}
    </time>
  )
}
function Reply({ post }: { post: Post }) {
  const [text, setText] = useState("")
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  const [sent, setSent] = useState("")
  // One request id per distinct reply: retrying the same click after a network error reuses
  // it, so the server applies the answer at most once (§4 request_id).
  const attempt = useRef<ReplyAttempt | null>(null)
  async function submit(action: ReplyAction) {
    setPending(true)
    setError("")
    const body = replyBody(post, action, text)
    attempt.current = nextAttempt(attempt.current, body)
    const result = await sendReply(post, body, attempt.current)
    if (!result.ok) {
      setError(result.error)
      setPending(false)
    } else if (result.post) {
      // Show the answered post now: this form unmounts in the same render (React batches the
      // two updates), with no interim "Reply sent" frame before the SSE event lands.
      applyLocalPost(result.post)
      setPending(false)
    } else {
      setSent("Reply sent")
      setPending(false)
    }
  }
  return (
    <form
      className="reply"
      onSubmit={(e) => {
        e.preventDefault()
        if (text.trim() && !pending && !sent) void submit("answer")
      }}
    >
      <div className="actions">
        <InteractiveHoverButton
          type="button"
          className="approve"
          disabled={pending || !!sent}
          onClick={() => submit("approve")}
        >
          Approve
        </InteractiveHoverButton>
        <InteractiveHoverButton
          type="button"
          className="reject"
          disabled={pending || !!sent}
          onClick={() => submit("reject")}
        >
          Reject
        </InteractiveHoverButton>
        <input
          aria-label="What should I do instead?"
          placeholder="What should I do instead?"
          value={text}
          maxLength={10000}
          onChange={(e) => setText(e.target.value)}
        />
        {text.trim() && (
          <InteractiveHoverButton type="submit" disabled={pending || !!sent}>
            Send answer
          </InteractiveHoverButton>
        )}
      </div>
      {(error || sent) && <span role="status">{error || sent}</span>}
    </form>
  )
}
/** The update card's headline. While the card's post is the open question its title is shown
 * above instead; once answered it moves into this (still mounted) card already revealed, rather
 * than blanking and replaying the blur-in on text that was just on screen. */
function CardTitle({ title, asQuestion }: { title: string | null; asQuestion: boolean }) {
  const wasQuestion = useRef(asQuestion)
  if (asQuestion) wasQuestion.current = true
  if (!title || asQuestion) return null
  return (
    <TextAnimate
      animation="blurInUp"
      by="character"
      once
      as="h2"
      {...(wasQuestion.current ? { initial: "show" } : {})}
    >
      {title}
    </TextAnimate>
  )
}
export function AgentDetail({ agent }: { agent: AgentNode }) {
  const question = agent.posts.find(
    (p) => p.status === "blocked" && !p.payload.reply && p.kind !== "log",
  )
  const questionIsMessage =
    question?.kind === "text" || question?.kind === "markdown"
  // Keep the pending decision above the most recent separate update.
  const update =
    question && !questionIsMessage && question.id === agent.latest?.id
      ? question
      : agent.posts.find((p) => p.id !== question?.id)
  return (
    <main className="workspace detail">
      <span aria-hidden="true" className="large-avatar">
        <Icon name={agent.avatar} />
      </span>
      <a className="back" href="#">
        ‹ All agents
      </a>
      <div className="hero">
        <h1><AgentName>{agent.name}</AgentName>{" "}<span className="detail-role">({agent.role})</span></h1>
        <p className="byline">
          Updated <Time value={agent.lastActivity} />
        </p>
      </div>
      <StatusLabel
        agent={question ? { ...agent, state: "needs-input" } : agent}
      />
      {question && (
        // Not the bare post id: the update card below can show the same post, and a duplicate
        // sibling key left this section orphaned on screen after the question was answered.
        <section className="decision" key={"question:" + question.id}>
          {questionIsMessage ? (
            <PostContent post={question} />
          ) : (
            <TextAnimate animation="blurInUp" by="character" once as="h2">
              {question.title || "Review requested"}
            </TextAnimate>
          )}
          <Reply post={question} />
        </section>
      )}
      {update && (
        <TweetCard className="update" key={update.id}>
          <CardTitle title={update.title} asQuestion={update.id === question?.id} />
          <PostContent post={update} />
          {update.payload.reply && (
            <p className="response">
              <strong>
                {
                  {
                    approve: "Approved",
                    reject: "Rejected",
                    answer: "Answered",
                  }[String(update.payload.reply.action)]
                }
              </strong>{" "}
              {update.payload.reply.text}
            </p>
          )}
        </TweetCard>
      )}
      {!agent.posts.length && <p className="byline">No updates yet.</p>}
      {!!agent.children.length && (
        <nav className="sub-links" aria-label="Subagents">
          {agent.children.map((c) => (
            <a key={c.id} href={"#agent/" + encodeURIComponent(c.id)}>
              <Icon name={c.avatar} size={18} />
              <AgentName>{c.name}</AgentName> ({c.role})
            </a>
          ))}
        </nav>
      )}
    </main>
  )
}
