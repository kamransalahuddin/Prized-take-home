import type { ReactNode } from "react"
import { Icon } from "@/components/icon"
import type { AgentNode } from "@/lib/types"
import { cn } from "@/lib/utils"

const LABEL: Record<AgentNode["state"], string> = {
  "needs-input": "Needs input",
  attention: "Requires attention",
  running: "Running",
  done: "Done",
}

function Dot({ className, pulse }: { className: string; pulse?: boolean }) {
  return (
    <span className="relative inline-flex size-4 shrink-0 items-center justify-center" aria-hidden="true">
      {pulse && (
        <span className={cn("absolute size-2 animate-ping rounded-full opacity-60 [animation-duration:1.6s]", className)} />
      )}
      <span className={cn("relative size-2 rounded-full", className)} />
    </span>
  )
}

export function StatusLabel({
  agent,
}: {
  agent: Pick<AgentNode, "state" | "status" | "live">
}) {
  const { state, status, live } = agent
  let mark: ReactNode
  if (state === "needs-input") mark = <Icon name="user-hand-up-bold" size={16} className="text-bad" />
  else if (state === "attention") mark = <Dot className={status === "warn" ? "bg-warn" : "bg-bad"} />
  else if (state === "running") mark = <Dot className="bg-busy" pulse={live} />
  else mark = <Dot className="bg-ok" />

  return (
    <div className="text-center text-[13px] leading-[18px] font-medium text-balance text-fg-2">
      <span className="relative -top-px mr-1.5 inline-flex align-middle">{mark}</span>
      {LABEL[state]}
    </div>
  )
}
