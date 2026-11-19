/// <reference types="node" />
import { describe, expect, it } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"
import { readFileSync } from "node:fs"
import { PostContent } from "./post-content"
import type { Post } from "@/lib/types"

// Vitest stubs CSS imports (even ?raw), so read the stylesheet from disk.
const css = readFileSync(new URL("../index.css", import.meta.url), "utf8")

const image: Post = {
  id: "p_img",
  agent: "lead",
  kind: "image",
  title: "Dashboard",
  status: "info",
  key: null,
  live: false,
  created_at: "2026-11-18T10:00:00.000Z",
  updated_at: "2026-11-18T10:00:00.000Z",
  seq: 1,
  gen: "g1",
  payload: { asset_id: "a_1", url: "/api/assets/a_1", mime: "image/svg+xml" },
} as Post

function rule(selector: string) {
  const at = css.indexOf("\n" + selector + " {")
  expect(at, selector + " rule").toBeGreaterThan(-1)
  return css.slice(at, css.indexOf("}", at)).replace(/\s+/g, " ")
}

describe("image posts", () => {
  it("wrap the image in a link that is not shrink-to-fit", () => {
    const html = renderToStaticMarkup(<PostContent post={image} />)
    expect(html).toMatch(/^<a class="post-image-link" href="\/api\/assets\/a_1"/)
    expect(html).toContain('<img class="post-image" loading="lazy" src="/api/assets/a_1" alt="Dashboard"/>')
    // The card centers its children; a centered (fit-content) link gives an SVG with only a
    // viewBox nothing to size against, and it renders 0x0. The link must take the card width.
    const link = rule(".post-image-link")
    expect(link).toContain("display: block;")
    expect(link).toContain("align-self: stretch;")
    expect(rule(".post-image")).toContain("max-width: 100%;")
  })
})
