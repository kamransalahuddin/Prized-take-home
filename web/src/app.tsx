import { useState } from "react"
import { AgentCardSkeleton } from "@/components/agent-card"
import { AgentDetail } from "@/components/agent-detail"
import { AgentTree } from "@/components/agent-tree"
import { Header } from "@/components/header"
import { Icon } from "@/components/icon"
import { TooltipProvider } from "@/components/ui/tooltip"
import { useBoard } from "@/lib/board"
import type { AgentNode } from "@/lib/types"

// Phone: two-column grid. sm and up: fixed-width columns in a centered, wrapping row.
const ROW =
  "grid grid-cols-2 items-start gap-x-3 gap-y-8 sm:flex sm:flex-wrap sm:justify-center sm:gap-x-4 sm:gap-y-12 lg:gap-x-12"
const COLUMN = "min-w-0 sm:w-[160px] lg:w-[200px]"

export function App() {
  const board = useBoard()
  const [selected, setSelected] = useState<AgentNode | null>(null)

  return (
    <TooltipProvider delayDuration={200}>
      <div className="flex min-h-dvh flex-col">
        <Header connection={board.connection} />
        <main className="mx-auto w-full max-w-6xl flex-1 px-4 pt-8 pb-16 sm:px-8 sm:pt-20">
          <h1 className="text-center text-xl leading-7 font-semibold tracking-[-0.01em] text-foreground">
            What are my agents up to?
          </h1>
          <div className="mt-8 sm:mt-12">
            {!board.loaded ? (
              <div className={ROW} aria-busy="true">
                {Array.from({ length: 4 }, (_, i) => (
                  <div key={i} className={COLUMN}>
                    <AgentCardSkeleton />
                  </div>
                ))}
              </div>
            ) : board.agents.length === 0 ? (
              <div className="flex flex-col items-center gap-4 pt-8 sm:pt-12">
                <Icon name="sleeping-circle-bold" size={40} className="text-faint" />
                <code className="rounded-lg bg-muted px-2.5 py-1 font-mono text-[13px] leading-[18px] text-muted-foreground">
                  board post -a lead "hello"
                </code>
              </div>
            ) : (
              <ul className={ROW}>
                {board.agents.map((agent) => (
                  <li key={agent.id} className={COLUMN}>
                    <AgentTree agent={agent} onSelect={setSelected} />
                  </li>
                ))}
              </ul>
            )}
          </div>
        </main>
      </div>
      <AgentDetail agent={selected} onOpenChange={(open) => !open && setSelected(null)} />
    </TooltipProvider>
  )
}
