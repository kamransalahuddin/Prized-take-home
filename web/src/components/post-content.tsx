import { useEffect, useMemo, useRef } from "react"
import type { Post } from "@/lib/types"

declare const markdownit: (options: object) => {
  render: (source: string) => string
}
declare const DOMPurify: { sanitize: (html: string) => string }

function Markdown({ source }: { source: string }) {
  const html = useMemo(
    () =>
      DOMPurify.sanitize(
        markdownit({ html: false, linkify: true }).render(source),
      ),
    [source],
  )
  return <div className="prose" dangerouslySetInnerHTML={{ __html: html }} />
}

function Log({ post }: { post: Post }) {
  const log = useRef<HTMLPreElement>(null)
  const follow = useRef(true)
  useEffect(() => {
    if (log.current && follow.current)
      log.current.scrollTop = log.current.scrollHeight
  }, [post.seq])
  return (
    <>
      <pre
        className="log"
        ref={log}
        onScroll={(e) => {
          const el = e.currentTarget
          follow.current = el.scrollHeight - el.clientHeight - el.scrollTop < 24
        }}
      >
        {post.payload.text}
      </pre>
      <small>
        {[
          post.live && "Streaming",
          post.payload.truncated && "Earlier output truncated",
          post.payload.exit_code != null && "Exit " + post.payload.exit_code,
        ]
          .filter(Boolean)
          .join(" · ")}
      </small>
    </>
  )
}
function Chart({ post }: { post: Post }) {
  const frame = useRef<HTMLIFrameElement>(null)
  const send = () =>
    frame.current?.contentWindow?.postMessage(
      { type: "board-chart", option: post.payload.option },
      "*",
    )
  useEffect(send, [post.payload.option])
  return (
    <iframe
      ref={frame}
      className="chart"
      sandbox="allow-scripts"
      src="/static/chart.html"
      title={post.title || "Agent chart"}
      loading="lazy"
      onLoad={send}
    />
  )
}
export function PostContent({ post }: { post: Post }) {
  const data = post.payload
  switch (post.kind) {
    case "markdown":
      return <Markdown source={data.markdown ?? ""} />
    case "chart":
      return <Chart post={post} />
    case "image":
      return (
        <a
          className="post-image-link"
          href={data.url}
          target="_blank"
          rel="noreferrer"
        >
          <img
            className="post-image"
            loading="lazy"
            src={data.url}
            alt={post.title || "Image posted by " + post.agent}
          />
        </a>
      )
    case "table":
      return (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                {data.columns.map((c: string, i: number) => (
                  <th key={i}>{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.rows.map((r: unknown[], i: number) => (
                <tr key={i}>
                  {r.map((v, j) => (
                    <td
                      key={j}
                      className={typeof v === "number" ? "numeric" : ""}
                    >
                      {String(v ?? "")}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    case "log":
      return <Log post={post} />
    default:
      return <p className="plain">{data.text}</p>
  }
}
