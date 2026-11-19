import type { AgentNode } from "@/lib/types"
import { StatusLabel } from "./status-label"
import { AgentCard } from "./agent-card"

export function AgentTree({
  agent,
  small = false,
  standalone = false,
}: {
  agent: AgentNode
  standalone?: boolean
  small?: boolean
}) {
  return (
    <div className="branch">
      <AgentCard agent={agent} small={small} />
      <StatusLabel agent={agent} />
      {!standalone && !!agent.children.length && (
        <ul className="branches">
          {agent.children.map((child) => (
            <li key={child.id}>
              <AgentTree agent={child} small />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
