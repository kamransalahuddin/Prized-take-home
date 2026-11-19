import type { ComponentProps } from "react"
import { Card } from "@/components/ui/card"
import { cn } from "@/lib/utils"

// MagicTweet's presentational shell, populated by local board entries.
export function TweetCard({
  className,
  ...props
}: ComponentProps<typeof Card>) {
  return (
    <Card
      className={cn(
        "relative flex h-fit w-full flex-col items-center gap-4 overflow-hidden rounded-xl border p-5 text-center",
        className,
      )}
      {...props}
    />
  )
}
