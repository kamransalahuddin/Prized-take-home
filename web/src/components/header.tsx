import { useLayoutEffect, useState, useSyncExternalStore } from "react"
import { Moon, Sun } from "lucide-react"
import type { Connection } from "@/lib/types"
import { PrizedMark } from "./prized-mark"
import { Badge } from "./ui/badge"
import { Button } from "./ui/button"

type Theme = "Light" | "Dark"

const systemTheme = matchMedia("(prefers-color-scheme: dark)")
const subscribeTheme = (notify: () => void) => {
  systemTheme.addEventListener("change", notify)
  return () => systemTheme.removeEventListener("change", notify)
}
const readSystemTheme = (): Theme => systemTheme.matches ? "Dark" : "Light"

export function Header({ connection }: { connection: Connection }) {
  const osTheme = useSyncExternalStore(subscribeTheme, readSystemTheme)
  const [override, setOverride] = useState<Theme | null>(() => {
    try {
      // Earlier builds stored the automatic OS choice as if it were a manual choice.
      const saved = localStorage.getItem("board-theme-override")
      if (saved === "Light" || saved === "Dark") return saved
    } catch { /* browser storage is optional */ }
    return null
  })
  const theme = override ?? osTheme
  useLayoutEffect(() => {
    document.documentElement.dataset.theme = theme.toLowerCase()
  }, [theme])

  function chooseTheme(choice: Theme) {
    const preference = choice === osTheme ? null : choice
    setOverride(preference)
    try {
      if (preference) localStorage.setItem("board-theme-override", preference)
      else localStorage.removeItem("board-theme-override")
    } catch { /* browser storage is optional */ }
  }
  const next = theme === "Dark" ? "Light" : "Dark"
  const Symbol = theme === "Dark" ? Moon : Sun
  return (
    <header className="mast">
      <nav aria-label="Board navigation">
        <a className="brand" href="#" aria-label="Prized agent board">
          <PrizedMark />
          Prized
        </a>
        {connection !== "live" && (
          <Badge variant="outline" role="status">
            {connection === "offline" ? "Reconnecting" : "Connecting"}
          </Badge>
        )}
        <Button
          variant="outline"
          type="button"
          className="theme-button"
          title={"Switch to " + next.toLowerCase() + " theme"}
          aria-label={"Theme: " + theme + ". Switch to " + next}
          onClick={() => chooseTheme(next)}
        >
          <Symbol size={13} />
          {theme}
        </Button>
      </nav>
    </header>
  )
}
