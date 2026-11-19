import { HyperText } from "./magicui/hyper-text"

export function AgentName({
  children,
  as = "span",
}: {
  children: string
  as?: "span" | "h1"
}) {
  return (
    <HyperText as={as} className="agent-name" animateOnHover={false}>
      {children}
    </HyperText>
  )
}
