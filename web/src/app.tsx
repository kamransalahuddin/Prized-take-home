import { useEffect, useState, useSyncExternalStore } from "react"
import { AgentDetail, postTitle, Time } from "@/components/agent-detail"
import { AgentTree } from "@/components/agent-tree"
import { PanSurface } from "@/components/pan-surface"
import { Header } from "@/components/header"
import { FlickeringGrid } from "@/components/magicui/flickering-grid"
import { TextAnimate } from "@/components/magicui/text-animate"
import { InteractiveHoverButton } from "@/components/magicui/interactive-hover-button"
import { TweetCard } from "@/components/magicui/tweet-card"
import { Icon } from "@/components/icon"
import { useBoard } from "@/lib/board"
import type { AgentNode } from "@/lib/types"

const phoneLayout = matchMedia("(max-width: 700px)")
function subscribeLayout(notify: () => void) {
  phoneLayout.addEventListener("change", notify)
  return () => phoneLayout.removeEventListener("change", notify)
}
const readPhoneLayout = () => phoneLayout.matches

function flatten(nodes: AgentNode[]): AgentNode[] {
  return nodes.flatMap((node) => [node, ...flatten(node.children)])
}
function readHash() {
  try {
    return decodeURIComponent(window.location.hash.slice(1))
  } catch {
    return ""
  }
}
export function App() {
  const phone = useSyncExternalStore(subscribeLayout, readPhoneLayout)
  const board = useBoard()
  const [route, setRoute] = useState(readHash)
  const [tab, setTab] = useState("Agent map")
  const [exploring, setExploring] = useState(false)
  useEffect(() => {
    const navigate = () => {
      setRoute(readHash())
      window.scrollTo(0, 0)
    }
    window.addEventListener("hashchange", navigate)
    return () => window.removeEventListener("hashchange", navigate)
  }, [])
  const nodes = flatten(board.agents)
  const byPath = new Map(nodes.map((n) => [n.id, n]))
  const selected = nodes.find((n) => route === "agent/" + n.id)
  const mapActive = !selected && tab === "Agent map" && nodes.length > 0
  return (
    <>
      <div aria-hidden="true" className="backdrop grid-field">
        <FlickeringGrid
          color="rgb(125,125,125)"
          squareSize={3}
          gridGap={10}
          flickerChance={0.12}
          maxOpacity={0.5}
        />
      </div>
      <Header connection={board.connection} />
      <PanSurface active={mapActive} onExploreChange={setExploring}>
        {selected ? (
          <AgentDetail key={selected.id} agent={selected} />
        ) : (
          <main className="workspace">
            <div className="hero" data-exploring={exploring || undefined} aria-hidden={exploring || undefined}>
              <TextAnimate animation="blurInUp" by="character" startOnView={false} animate={exploring ? "hidden" : "show"} as="h1">
                What are my agents up to?
              </TextAnimate>
            </div>
            {nodes.length > 0 && (
              <div className="switcher">
                {["Agent map", "Activity"].map((t) => (
                  <InteractiveHoverButton
                    key={t}
                    aria-pressed={tab === t}
                    onClick={() => setTab(t)}
                  >
                    {t}
                  </InteractiveHoverButton>
                ))}
              </div>
            )}
            {!board.loaded || !nodes.length ? (
              <section className="empty">
                <Icon name="user-astronaut" size={70} />
                <TextAnimate animation="blurInUp" by="character" once as="h2">
                  {board.loaded
                    ? "Waiting for the first agent"
                    : board.connection === "offline"
                      ? "Reconnecting to the board"
                      : "Connecting to the board"}
                </TextAnimate>
                {board.loaded && (
                  <code>board post -a lead "Starting work"</code>
                )}
              </section>
            ) : tab === "Agent map" ? (
              <ul className="forest">
                {(phone ? nodes : board.agents).map((agent) => (
                  <li key={agent.id}>
                    <AgentTree agent={agent} standalone={phone} />
                  </li>
                ))}
              </ul>
            ) : (
              <ul className="activity">
                {board.posts.map((p) => {
                  const node = byPath.get(
                    p.agent.split("/").filter(Boolean).join("/"),
                  )!
                  return (
                    <li key={p.id}>
                      <TweetCard>
                        <a href={"#agent/" + encodeURIComponent(node.id)}>
                          <Icon name={node.avatar} size={28} />
                          <strong>
                            {node.name} · {node.role}
                          </strong>
                          <span>{postTitle(p)}</span>
                          <Time value={p.updated_at} />
                        </a>
                      </TweetCard>
                    </li>
                  )
                })}
              </ul>
            )}
          </main>
        )}
      </PanSurface>
    </>
  )
}
