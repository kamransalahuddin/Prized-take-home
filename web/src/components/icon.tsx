import icons from "../../../board/static/icons/solar.json"
import { cn } from "@/lib/utils"

const SET = icons as Record<string, string>

/** Solar Bold icon by name ("ghost-bold" or "ghost"). Takes the text color. */
export function Icon({ name, size = 20, className }: { name: string; size?: number; className?: string }) {
  const svg = SET[name] ?? SET[`${name}-bold`]
  if (!svg) return null
  return (
    <span
      aria-hidden="true"
      className={cn("inline-flex shrink-0 [&>svg]:size-full", className)}
      style={{ width: size, height: size }}
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  )
}
