import { Icon } from "@/components/icon"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import type { Connection } from "@/lib/types"
import { cn } from "@/lib/utils"

const HINT: Record<Connection, string> = {
  live: "Live",
  connecting: "Connecting",
  offline: "Connection lost",
}

function ConnectionIndicator({ connection }: { connection: Connection }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          role="status"
          aria-label={HINT[connection]}
          tabIndex={0}
          className="inline-flex size-8 items-center justify-center rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-primary"
        >
          {connection === "offline" ? (
            <Icon name="expressionless-circle-bold" size={20} className="text-muted-foreground" />
          ) : (
            <span
              className={cn(
                "size-2 rounded-full",
                connection === "live" ? "bg-busy" : "animate-pulse bg-faint",
              )}
            />
          )}
        </span>
      </TooltipTrigger>
      <TooltipContent side="bottom" sideOffset={4}>
        {HINT[connection]}
      </TooltipContent>
    </Tooltip>
  )
}

export function Header({ connection }: { connection: Connection }) {
  return (
    <header className="relative flex h-14 items-center justify-center px-4">
      <span className="text-[15px] leading-6 font-semibold tracking-[-0.03em] text-foreground select-none">Prized</span>
      <div className="absolute inset-y-0 right-3 flex items-center sm:right-5">
        <ConnectionIndicator connection={connection} />
      </div>
    </header>
  )
}
