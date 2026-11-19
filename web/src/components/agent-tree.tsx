import type { ReactNode } from "react"
import { AgentCard } from "@/components/agent-card"
import { StatusLabel } from "@/components/status-label"
import type { AgentNode } from "@/lib/types"
import { cn } from "@/lib/utils"

// Connector geometry (CSS vars so phone and desktop can differ):
//   --rail   x of the dashed rail, measured from the parent card's left edge
//   --indent how far a child card is pushed right of its parent
//   --drop   vertical gap above each child card
const GEOMETRY = "[--rail:14px] [--indent:26px] [--drop:16px] sm:[--rail:22px] sm:[--indent:40px] sm:[--drop:20px]"
const LINE = "border-dashed border-faint/80 dark:border-faint"

type Props = { agent: AgentNode; onSelect: (agent: AgentNode) => void }

/** A top-level agent with its subagents hanging beneath it. */
export function AgentTree({ agent, onSelect }: Props) {
  return (
    <div className={cn("flex w-full flex-col", GEOMETRY)}>
      <Node agent={agent} size="default" onSelect={onSelect} />
    </div>
  )
}

function Node({
  agent,
  size,
  connector,
  onSelect,
}: Props & { size: "default" | "sm"; connector?: ReactNode }) {
  const hasChildren = agent.children.length > 0
  return (
    <>
      <div className="relative">
        {connector}
        <AgentCard agent={agent} size={size} onSelect={onSelect} />
      </div>
      <div className="relative pt-2">
        {hasChildren && <span aria-hidden="true" className={cn("absolute inset-y-0 left-(--rail) border-l", LINE)} />}
        <StatusLabel agent={agent} size={size} className="pl-[calc(var(--rail)+6px)]" />
      </div>
      {hasChildren && (
        <ul className="flex flex-col">
          {agent.children.map((child, i) => (
            <Branch key={child.id} agent={child} last={i === agent.children.length - 1} onSelect={onSelect} />
          ))}
        </ul>
      )}
    </>
  )
}

function Branch({ agent, last, onSelect }: Props & { last: boolean }) {
  return (
    <li className="relative flex flex-col pt-(--drop) pl-(--indent)">
      {/* Rail continues past this child to its next sibling. */}
      {!last && <span aria-hidden="true" className={cn("absolute inset-y-0 left-(--rail) border-l", LINE)} />}
      <Node
        agent={agent}
        size="sm"
        onSelect={onSelect}
        connector={
          last ? (
          // Rounded elbow: down from the rail, then right into the card's middle.
          <span
            aria-hidden="true"
            className={cn(
              "absolute -top-(--drop) right-full h-[calc(50%+var(--drop))] w-[calc(var(--indent)-var(--rail))] rounded-bl-[10px] border-b border-l",
              LINE,
            )}
          />
        ) : (
          <span
            aria-hidden="true"
            className={cn("absolute top-1/2 right-full w-[calc(var(--indent)-var(--rail))] border-t", LINE)}
          />
          )
        }
      />
    </li>
  )
}
