import { AgentName } from "./agent-name"
import { Fragment, useEffect, useRef, useState } from "react"
import { TextAnimate } from "./magicui/text-animate"
import { InteractiveHoverButton } from "./magicui/interactive-hover-button"
import { TweetCard } from "./magicui/tweet-card"
import { Icon } from "./icon"
import { StatusLabel } from "./status-label"
import { PostContent } from "./post-content"
import { avatarFor, compareNewest, displayName, isQuestion, roleLabel } from "@/lib/agents"
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
/** The update card's headline. A just-answered question moves from the decision area into
 * this card: show its title already revealed instead of blanking and replaying the blur-in on
 * text that was just on screen. */
function CardTitle({ title, revealed }: { title: string | null; revealed: boolean }) {
  if (!title) return null
  return (
    <TextAnimate
      animation="blurInUp"
      by="character"
      once
      as="h2"
      {...(revealed ? { initial: "show" } : {})}
    >
      {title}
    </TextAnimate>
  )
}
/** Link to the subagent a post came from, shown when a parent's page carries it. */
function From({ id }: { id: string }) {
  return (
    <p className="byline from">
      <a href={"#agent/" + encodeURIComponent(id)}>
        <Icon name={avatarFor(id)} size={18} />
        <AgentName>{displayName(id)}</AgentName> ({roleLabel(id)})
      </a>
    </p>
  )
}
/** Every post of an agent and its subagents, newest first. */
function treePosts(agent: AgentNode): Post[] {
  return [agent.posts, ...agent.children.map(treePosts)].flat().sort(compareNewest)
}
/** How many updates the page shows; the rest are on each subagent's own page. */
const MAX_UPDATES = 60
export function AgentDetail({ agent }: { agent: AgentNode }) {
  const all = treePosts(agent)
  // Every open question (blocked, unanswered, any kind) gets its own Approve/Reject, so the
  // "Needs input" label always comes with a way to answer (agents.ts decides both).
  const questions = agent.questions
  // Below the decisions: what the agent and its subagents posted (charts, images, tables,
  // logs, ...), newest first. A subagent's open question is answered on its own page.
  const updates = all.filter((p) => !isQuestion(p)).slice(0, MAX_UPDATES)
  // A parent that never posted shows the post behind its label.
  if (!updates.length && agent.state === "attention" && agent.cause) updates.push(agent.cause)
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
      <StatusLabel agent={agent} />
      {questions.map((question) => {
        const isMessage = question.kind === "text" || question.kind === "markdown"
        return (
          // A re-ask with -k keeps the id but changes gen: start a fresh reply form for it.
          // Prefixed: the update card below is keyed by a post id, and a sibling with the same
          // key left this section orphaned on screen after the question was answered.
          <Fragment key={"question:" + question.id + ":" + (question.gen ?? "")}>
            {question.agent !== agent.id && <From id={question.agent} />}
            <section className="decision">
              {isMessage ? (
                <PostContent post={question} />
              ) : (
                <TextAnimate animation="blurInUp" by="character" once as="h2">
                  {question.title || "Review requested"}
                </TextAnimate>
              )}
              <Reply post={question} />
            </section>
            {!isMessage && (
              // The table, chart, image or log under review, styled like an update.
              <TweetCard className="update">
                <PostContent post={question} />
              </TweetCard>
            )}
          </Fragment>
        )
      })}
      {updates.map((update) => (
        <Fragment key={update.id}>
          {update.agent !== agent.id && <From id={update.agent} />}
          <TweetCard className="update">
            <CardTitle title={update.title} revealed={!!update.payload.reply} />
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
        </Fragment>
      ))}
      {!questions.length && !updates.length && <p className="byline">No updates yet.</p>}
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
