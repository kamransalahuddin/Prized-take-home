// Placeholder for the agent detail screen. Replace this file with your own screen;
// the home page only relies on the props below.
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog"
import type { AgentNode } from "@/lib/types"

export interface AgentDetailProps {
  agent: AgentNode | null // null = closed
  onOpenChange: (open: boolean) => void
}

export function AgentDetail({ agent, onOpenChange }: AgentDetailProps) {
  return (
    <Dialog open={agent !== null} onOpenChange={onOpenChange}>
      <DialogContent className="rounded-xl">
        <DialogTitle>{agent?.name}</DialogTitle>
      </DialogContent>
    </Dialog>
  )
}
