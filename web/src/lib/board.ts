// TEMP: replaced by fe-data branch. Returns fixtures so the UI can be built in isolation.
// Supports ?fixture=empty | offline | connecting | loading | deep.
import { useMemo } from "react"
import { fixtureState } from "@/dev/fixtures"
import type { BoardState } from "@/lib/types"

export function useBoard(): BoardState {
  return useMemo(() => fixtureState(new URLSearchParams(window.location.search).get("fixture")), [])
}
