const isDark = () => matchMedia("(prefers-color-scheme: dark)").matches
var CARTESIAN = {
  line: 1,
  bar: 1,
  scatter: 1,
  effectScatter: 1,
  candlestick: 1,
  boxplot: 1,
  pictorialBar: 1,
  heatmap: 1,
}
var PALETTE = {
  light: [
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#6250d6",
    "#e34948",
  ],
  dark: [
    "#3987e5",
    "#d95926",
    "#199e70",
    "#c98500",
    "#d55181",
    "#0ca30c",
    "#9085e9",
    "#e66767",
  ],
}
function tokens() {
  var d = isDark()
  return {
    dark: d,
    palette: d ? PALETTE.dark : PALETTE.light,
    fg: d ? "#e3e3e6" : "#2f2f31",
    muted: d ? "#959597" : "#5c5c5e",
    faint: d ? "#565759" : "#9c9d9f",
    axis: d ? "#2e2f31" : "#dedede",
    grid: d ? "#1f2022" : "#f0f0f1",
    card: d ? "#1a1a1b" : "#ffffff",
    border: d ? "#2a2b2d" : "#dedede",
    font: 'Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif',
  }
}
function isObj(v) {
  return v && typeof v === "object" && !Array.isArray(v)
}
function fill(target, key, defaults) {
  // Set target[key] to defaults when unset; when it is an object, fill only its unset keys (deep).
  if (target[key] === undefined) {
    target[key] = defaults
    return target[key]
  }
  if (isObj(target[key]) && isObj(defaults)) {
    Object.keys(defaults).forEach(function (k) {
      fill(target[key], k, defaults[k])
    })
  }
  return target[key]
}
function asArray(v) {
  return v == null ? [] : Array.isArray(v) ? v : [v]
}
function eachAxis(o, key, fn) {
  if (Array.isArray(o[key]))
    o[key].forEach(function (a) {
      if (isObj(a)) fn(a)
    })
  else if (isObj(o[key])) fn(o[key])
}
function chartOption(raw) {
  var o = JSON.parse(JSON.stringify(raw || {})) // untrusted JSON: cloned, never evaluated
  var t = tokens()
  var series = asArray(o.series).filter(isObj)
  var points = 0
  series.forEach(function (s) {
    if (Array.isArray(s.data)) points = Math.max(points, s.data.length)
  })
  asArray(o.dataset).forEach(function (d) {
    if (isObj(d) && Array.isArray(d.source))
      points = Math.max(points, d.source.length)
  })
  var cartesian =
    !!(o.xAxis || o.yAxis) ||
    series.some(function (s) {
      return CARTESIAN[s.type || "line"]
    })
  var axisTrigger =
    cartesian &&
    series.some(function (s) {
      return s.type !== "scatter" && s.type !== "effectScatter"
    })
  var named = series.filter(function (s) {
    return s.name != null && s.name !== ""
  })
  var wantLegend = o.legend === undefined && named.length >= 2

  fill(o, "backgroundColor", "transparent")
  fill(o, "color", t.palette)
  fill(o, "textStyle", { fontFamily: t.font, color: t.muted, fontSize: 12 })
  if (points > 2000) fill(o, "animation", false)
  else fill(o, "animationDuration", 400)

  fill(o, "tooltip", { trigger: axisTrigger ? "axis" : "item" })
  asArray(o.tooltip).forEach(function (tt) {
    if (!isObj(tt)) return
    fill(tt, "backgroundColor", t.card)
    fill(tt, "borderColor", t.border)
    fill(tt, "borderWidth", 1)
    fill(tt, "padding", [6, 10])
    fill(tt, "textStyle", { color: t.fg, fontSize: 12, fontFamily: t.font })
    fill(
      tt,
      "extraCssText",
      "border-radius:8px;box-shadow:0 8px 24px -6px rgba(0,0,0,.25);",
    )
    fill(tt, "confine", true)
    if (tt.trigger === "axis")
      fill(tt, "axisPointer", {
        type: "line",
        lineStyle: { color: t.faint, width: 1 },
      })
  })

  if (wantLegend)
    o.legend = {
      type: "scroll",
      top: 0,
      left: 0,
      icon: "roundRect",
      itemWidth: 10,
      itemHeight: 10,
      itemGap: 14,
    }
  asArray(o.legend).forEach(function (lg) {
    if (!isObj(lg)) return
    fill(lg, "textStyle", { color: t.muted, fontSize: 12 })
    fill(lg, "pageTextStyle", { color: t.muted })
    fill(lg, "pageIconColor", t.muted)
    fill(lg, "pageIconInactiveColor", t.faint)
    fill(lg, "inactiveColor", t.faint)
  })
  asArray(o.title).forEach(function (ti) {
    if (!isObj(ti)) return
    fill(ti, "textStyle", { color: t.fg, fontSize: 13, fontWeight: 600 })
    fill(ti, "subtextStyle", { color: t.muted })
  })

  if (cartesian && o.grid === undefined && !o.polar && !o.radar) {
    var yNamed = false
    eachAxis(o, "yAxis", function (a) {
      if (a.name) yNamed = true
    })
    var xNamed = false
    eachAxis(o, "xAxis", function (a) {
      if (a.name) xNamed = true
    })
    eachAxis(o, "xAxis", function (a) {
      if (a.name) {
        fill(a, "nameLocation", "middle")
        fill(a, "nameGap", 28)
      }
    })
    o.grid = {
      left: 4,
      right: 16,
      bottom: xNamed ? 22 : 4,
      containLabel: true,
      top: (o.legend ? 34 : 12) + (yNamed ? 22 : 0),
    }
  }
  ;["xAxis", "yAxis", "radiusAxis", "angleAxis", "singleAxis"].forEach(
    function (k) {
      eachAxis(o, k, function (a) {
        fill(a, "axisLine", { lineStyle: { color: t.axis } })
        fill(a, "axisTick", { lineStyle: { color: t.axis } })
        fill(a, "axisLabel", {
          color: t.muted,
          fontSize: 11,
          hideOverlap: true,
        })
        fill(a, "splitLine", { lineStyle: { color: t.grid } })
        fill(a, "nameTextStyle", { color: t.faint, fontSize: 11 })
        if (a.type === "value" || (a.type === undefined && k === "yAxis"))
          fill(a, "axisLine", { show: false })
      })
    },
  )
  if (isObj(o.radar)) {
    fill(o.radar, "axisName", { color: t.muted })
    fill(o.radar, "splitLine", { lineStyle: { color: t.grid } })
    fill(o.radar, "axisLine", { lineStyle: { color: t.axis } })
    fill(o.radar, "splitArea", { show: false })
  }
  if (isObj(o.visualMap)) fill(o.visualMap, "textStyle", { color: t.muted })

  if (cartesian && points > 500 && o.dataZoom === undefined) {
    o.dataZoom = [
      {
        type: "inside",
        throttle: 50,
        preventDefaultMouseMove: false,
        zoomOnMouseWheel: "shift",
      },
    ]
  }
  series.forEach(function (s) {
    var type = s.type || "line"
    var big = Array.isArray(s.data) ? s.data.length : points
    if (type === "line") {
      fill(s, "lineStyle", { width: 2 })
      if (big > 60) fill(s, "showSymbol", false)
      fill(s, "symbolSize", 6)
      if (big > 2000) fill(s, "sampling", "lttb")
    } else if (type === "bar") {
      fill(s, "barMaxWidth", 28)
      if (!s.stack) fill(s, "itemStyle", { borderRadius: [3, 3, 0, 0] })
      if (big > 2000) fill(s, "large", true)
    } else if (type === "scatter") {
      fill(s, "symbolSize", 7)
      fill(s, "itemStyle", { opacity: 0.8 })
      if (big > 2000) fill(s, "large", true)
    } else if (type === "pie") {
      fill(s, "itemStyle", {
        borderColor: t.card,
        borderWidth: 2,
        borderRadius: 3,
      })
      fill(s, "label", { color: t.fg })
    }
  })
  return o
}

const host = document.getElementById("plot")
let option
let chart
function draw() {
  try {
    if (!chart) chart = echarts.init(host)
    chart.setOption(chartOption(option), true)
  } catch {
    chart?.dispose()
    chart = null
    host.textContent = "Could not draw this chart."
  }
}
window.addEventListener("message", (event) => {
  if (event.source !== parent || event.data?.type !== "board-chart") return
  option = event.data.option
  draw()
})
matchMedia("(prefers-color-scheme: dark)").addEventListener(
  "change",
  () => option && draw(),
)
new ResizeObserver(() => chart?.resize()).observe(host)
