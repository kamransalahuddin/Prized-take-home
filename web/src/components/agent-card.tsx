import { useEffect, useState } from "react"
import { AgentName } from "./agent-name"
import type { AgentNode } from "@/lib/types"
import { Icon } from "./icon"
import { TweetCard } from "./magicui/tweet-card"
import { CardTitle } from "./ui/card"

// Map cards remount when returning from Activity or an agent detail page.
const seenMapAgents = new Set<string>()

export function AgentCard({
  agent,
  small,
}: {
  agent: AgentNode
  small: boolean
}) {
  const [revealName] = useState(() => !seenMapAgents.has(agent.id))
  useEffect(() => {
    seenMapAgents.add(agent.id)
  }, [agent.id])

  return (
    <TweetCard className={"identity " + (small ? "small" : "")}>
      <a
        className="card-link"
        href={"#agent/" + encodeURIComponent(agent.id)}
        aria-label={agent.name + ", " + agent.id}
      >
        <CardTitle>
          {revealName ? <AgentName>{agent.name}</AgentName> : agent.name}{" "}
          <span className="card-role">({agent.role})</span>
        </CardTitle>
        <span className="portrait">
          <Icon name={agent.avatar} />
        </span>
      </a>
    </TweetCard>
  )
}
