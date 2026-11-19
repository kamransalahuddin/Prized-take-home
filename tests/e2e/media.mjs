// Browser check for image and chart posts on the agent detail page.
//
//   cd tests/e2e && npm i && node media.mjs
//
// Starts `board serve` on a throwaway BOARD_DIR, posts every image format (plus SVGs with no
// intrinsic size, a 4000x3000 JPEG and a tall PNG) and five charts, then opens each agent at
// 390x844 and 1440x900 in light and dark. It never scrolls: every post is the first thing in
// its card, so it must load and show at a nonzero size where it is. Exits 1 on any failure.
//
// Env: PYTHON (default: python3; needs `board` importable), ENGINE (chromium | webkit | firefox),
// CHROME (a Chrome binary; default: the macOS install if present, else Playwright's own),
// PORT (default 8871), SHOTS (directory to save a screenshot of each card).
import * as pw from "playwright-core"
import { spawn, execFileSync } from "node:child_process"
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join, dirname } from "node:path"
import { fileURLToPath } from "node:url"

const here = dirname(fileURLToPath(import.meta.url))
const root = join(here, "..", "..")
const python = process.env.PYTHON || "python3"
const engine = process.env.ENGINE || "chromium"
const port = process.env.PORT || "8871"
const shots = process.env.SHOTS
const base = `http://127.0.0.1:${port}`
const macChrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
const chrome = process.env.CHROME || (existsSync(macChrome) ? macChrome : undefined)

const dir = mkdtempSync(join(tmpdir(), "board-e2e-"))
const env = { ...process.env, BOARD_DIR: dir, PYTHONPATH: root }
delete env.BOARD_URL
const board = (...args) => execFileSync(python, ["-m", "board.cli", ...args], { env, encoding: "utf8" }).trim()

const svg = (attrs, label, fill) =>
  `<svg xmlns="http://www.w3.org/2000/svg" ${attrs}><rect width="320" height="180" fill="${fill}"/>` +
  `<text x="20" y="100" font-size="28" fill="#fff">${label}</text></svg>`
const files = {
  "sample.svg": svg('width="320" height="180"', "SVG 320x180", "#26a544"),
  // No intrinsic width: these collapsed to 0x0 in the shrink-to-fit image link.
  "viewbox.svg": svg('viewBox="0 0 320 180"', "viewBox only", "#2563eb"),
  "percent.svg": svg('width="100%" height="100%" viewBox="0 0 320 180"', "width 100%", "#9333ea"),
}
for (const [name, text] of Object.entries(files)) writeFileSync(join(dir, name), text)

const browser = await pw[engine].launch(engine === "chromium" && chrome ? { executablePath: chrome } : {})
const failures = []
// Let the title finish its blur-in before a screenshot.
const shoot = async (page, opts) => (await page.waitForTimeout(900), page.locator(".update").screenshot(opts))
let server
try {
  // Large and tall rasters, drawn by the browser so the repo needs no image tooling.
  const scratch = await browser.newPage()
  for (const [name, w, h, type] of [["big.jpg", 4000, 3000, "image/jpeg"], ["tall.png", 200, 2000, "image/png"]]) {
    const data = await scratch.evaluate(([w, h, type]) => {
      const c = Object.assign(document.createElement("canvas"), { width: w, height: h })
      const g = c.getContext("2d")
      const grad = g.createLinearGradient(0, 0, w, h)
      grad.addColorStop(0, "#e11d48")
      grad.addColorStop(1, "#1d4ed8")
      g.fillStyle = grad
      g.fillRect(0, 0, w, h)
      return c.toDataURL(type, 0.6).split(",")[1]
    }, [w, h, type])
    writeFileSync(join(dir, name), Buffer.from(data, "base64"))
  }
  await scratch.close()

  const images = {
    png: join(here, "fixtures/sample.png"),
    jpg: join(here, "fixtures/sample.jpg"),
    gif: join(here, "fixtures/sample.gif"),
    webp: join(here, "fixtures/sample.webp"),
    svg: join(dir, "sample.svg"),
    "svg-viewbox": join(dir, "viewbox.svg"),
    "svg-percent": join(dir, "percent.svg"),
    big: join(dir, "big.jpg"),
    tall: join(dir, "tall.png"),
  }
  for (const [name, file] of Object.entries(images)) board("post", "-a", "img-" + name, "-t", name + " image", "--image", file)

  const x = Array.from({ length: 5000 }, (_, i) => i)
  const charts = {
    line: { xAxis: { type: "category", data: ["a", "b", "c", "d"] }, yAxis: {}, series: [{ type: "line", data: [3, 7, 4, 9] }] },
    bar: { xAxis: { type: "category", data: ["api", "db", "web"] }, yAxis: {}, series: [{ type: "bar", data: [12, 30, 22] }] },
    scatter: { xAxis: {}, yAxis: {}, series: [{ type: "scatter", data: x.slice(0, 500).map((i) => [i, (i * 7919) % 101]) }] },
    pie: { series: [{ type: "pie", data: [{ name: "pass", value: 214 }, { name: "fail", value: 3 }] }] },
    big: { xAxis: { type: "category", data: x }, yAxis: {}, series: [0, 1, 2].map((s) => ({ type: "line", data: x.map((i) => Math.sin(i / 50 + s) * 100) })) },
  }
  for (const [name, option] of Object.entries(charts)) {
    writeFileSync(join(dir, "chart.json"), JSON.stringify(option))
    board("post", "-a", "chart-" + name, "-t", name + " chart", "--chart", "@" + join(dir, "chart.json"))
  }

  server = spawn(python, ["-m", "board.cli", "serve", "--port", port, "--data", dir], { env, stdio: "ignore" })
  for (let i = 0; ; i++) {
    try {
      if ((await fetch(base + "/api/posts?limit=1")).ok) break
    } catch {}
    if (i > 100) throw new Error("board serve did not start on " + base)
    await new Promise((r) => setTimeout(r, 100))
  }

  for (const [width, height] of [[390, 844], [1440, 900]]) {
    for (const colorScheme of ["light", "dark"]) {
      const view = `${width}-${colorScheme}`
      const page = await browser.newPage({ viewport: { width, height }, colorScheme })
      page.on("pageerror", (e) => failures.push(`${view}: page error ${e.message}`))
      for (const name of Object.keys(images)) {
        const where = `${view} ${name}`
        await page.goto(`${base}/#agent/img-${name}`)
        const img = page.locator(".update img.post-image")
        try {
          await img.waitFor({ state: "attached", timeout: 5000 })
          await page.waitForFunction(() => {
            const i = document.querySelector(".update img.post-image")
            return i?.complete && i.naturalWidth > 0
          }, null, { timeout: 5000 })
        } catch {
          failures.push(`${where}: image never loaded without scrolling`)
          continue
        }
        const m = await img.evaluate((i) => {
          const r = i.getBoundingClientRect()
          const card = i.closest(".update").getBoundingClientRect()
          return {
            w: r.width, h: r.height, left: r.left, right: r.right, nw: i.naturalWidth, nh: i.naturalHeight,
            cardLeft: card.left, cardRight: card.right, scrollWidth: document.documentElement.scrollWidth,
          }
        })
        const ratio = m.w / m.h
        const natural = m.nw / m.nh
        if (!(m.w >= 40 && m.h >= 40)) failures.push(`${where}: rendered ${m.w}x${m.h} (natural ${m.nw}x${m.nh})`)
        else if (Math.abs(ratio - natural) / natural > 0.02) failures.push(`${where}: aspect ${ratio.toFixed(3)} != ${natural.toFixed(3)}`)
        if (m.left < m.cardLeft - 0.5 || m.right > m.cardRight + 0.5) failures.push(`${where}: image wider than its card`)
        if (m.scrollWidth > width) failures.push(`${where}: page scrolls sideways (${m.scrollWidth}px)`)
        if (shots) await shoot(page, { path: join(shots, `img-${name}-${view}.png`) })
      }
      for (const name of Object.keys(charts)) {
        const where = `${view} chart ${name}`
        await page.goto(`${base}/#agent/chart-${name}`)
        const iframe = page.locator(".update iframe.chart")
        try {
          await iframe.waitFor({ timeout: 5000 })
          const frame = await (await iframe.elementHandle()).contentFrame()
          await frame.waitForFunction(() => {
            const c = document.querySelector("#plot canvas")
            if (!c || !c.width || !c.height) return false
            const px = c.getContext("2d").getImageData(0, 0, c.width, c.height).data
            for (let i = 3; i < px.length; i += 4 * 97) if (px[i]) return true
            return false
          }, null, { timeout: 10000 })
          const box = await iframe.boundingBox()
          if (!(box.width > 100 && box.height > 100)) failures.push(`${where}: frame ${box.width}x${box.height}`)
        } catch (e) {
          failures.push(`${where}: chart did not draw (${String(e.message).split("\n")[0]})`)
        }
        if (shots) await shoot(page, { path: join(shots, `chart-${name}-${view}.png`) })
      }
      await page.close()
    }
  }
} finally {
  await browser.close()
  server?.kill()
  rmSync(dir, { recursive: true, force: true })
}
if (failures.length) {
  console.error(failures.join("\n"))
  console.error(`\n${failures.length} media check(s) failed (${engine})`)
  process.exit(1)
}
console.log(`media.mjs: all image and chart checks passed (${engine})`)
