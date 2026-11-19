import { AgentName } from "./agent-name"
import { useEffect, useRef, useState } from "react"
import { TextAnimate } from "./magicui/text-animate"
import { InteractiveHoverButton } from "./magicui/interactive-hover-button"
import { TweetCard } from "./magicui/tweet-card"
import { Icon } from "./icon"
import { StatusLabel } from "./status-label"
import { PostContent } from "./post-content"
import type { AgentNode, Post } from "@/lib/types"

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
  const attempt = useRef<{ key: string; id: string } | null>(null)
  async function submit(action: string) {
    setPending(true)
    setError("")
    const body = { action, text: text.trim() || null, expect_gen: post.gen }
    const key = JSON.stringify(body)
    if (attempt.current?.key !== key)
      attempt.current = { key, id: crypto.randomUUID?.() ?? String(Math.random()).slice(2) + Date.now() }
    try {
      const response = await fetch("/api/posts/" + encodeURIComponent(post.id) + "/reply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...body, request_id: attempt.current.id }),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(data.error || "Could not send reply")
      setSent("Reply sent")
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not send reply")
    } finally {
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
        <section className="decision" key={question.id}>
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
          {update.title && !(update.id === question?.id) && (
            <TextAnimate animation="blurInUp" by="character" once as="h2">{update.title}</TextAnimate>
          )}
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
