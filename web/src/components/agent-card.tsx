import { Card } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Icon } from "@/components/icon"
import type { AgentNode } from "@/lib/types"
import { cn } from "@/lib/utils"

type Size = "default" | "sm"

const SIZES = {
  default: {
    card: "gap-2.5 p-3 sm:gap-3 sm:p-4",
    name: "text-[15px] leading-6 font-semibold",
    tile: "size-16 sm:size-20 lg:size-22",
    icon: "size-9! sm:size-11! lg:size-12!",
    role: "text-[13px] leading-[18px]",
  },
  sm: {
    card: "gap-2 p-2.5 sm:p-3",
    name: "text-[13px] leading-[18px] font-semibold",
    tile: "size-12 sm:size-14",
    icon: "size-6! sm:size-7!",
    role: "text-xs leading-4",
  },
} satisfies Record<Size, Record<string, string>>

/** One agent: name, avatar tile, role. The whole card is a button. */
export function AgentCard({
  agent,
  size = "default",
  onSelect,
}: {
  agent: AgentNode
  size?: Size
  onSelect: (agent: AgentNode) => void
}) {
  const s = SIZES[size]
  return (
    <Card
      role="button"
      tabIndex={0}
      aria-label={`${agent.name}, ${agent.role}`}
      onClick={() => onSelect(agent)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault()
          onSelect(agent)
        }
      }}
      className={cn(
        "w-full cursor-pointer items-center text-center shadow-resting ring-border outline-none select-none",
        "transition-colors duration-150 hover:bg-[color-mix(in_srgb,var(--accent)_55%,var(--card))]",
        "focus-visible:ring-2 focus-visible:ring-primary",
        s.card,
      )}
    >
      <div className={cn("w-full truncate text-foreground", s.name)}>{agent.name}</div>
      <div className={cn("flex items-center justify-center rounded-lg bg-muted text-muted-foreground dark:bg-accent", s.tile)}>
        <Icon name={agent.avatar} className={s.icon} />
      </div>
      <div className={cn("w-full truncate text-muted-foreground", s.role)}>{agent.role}</div>
    </Card>
  )
}

export function AgentCardSkeleton() {
  return (
    <div className="flex w-full flex-col items-center gap-2.5">
      <Card className="w-full items-center gap-2.5 p-3 shadow-resting ring-border sm:gap-3 sm:p-4">
        <Skeleton className="h-4 w-14 rounded-sm" />
        <Skeleton className="size-16 rounded-lg sm:size-20 lg:size-22" />
        <Skeleton className="h-3.5 w-20 rounded-sm" />
      </Card>
      <Skeleton className="h-3.5 w-24 rounded-sm bg-accent" />
    </div>
  )
}
