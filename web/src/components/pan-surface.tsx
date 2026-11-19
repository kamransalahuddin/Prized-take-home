import {
  useLayoutEffect,
  useRef,
  useState,
  type PointerEvent,
  type ReactNode,
  type TouchEvent,
} from "react"

interface Drag {
  id: number
  x: number
  y: number
  left: number
  top: number
}

export function PanSurface({
  active,
  children,
  onExploreChange,
}: {
  active: boolean
  children: ReactNode
  onExploreChange: (dragging: boolean) => void
}) {
  const viewport = useRef<HTMLDivElement>(null)
  const plane = useRef<HTMLDivElement>(null)
  const [overflows, setOverflows] = useState(false)
  const canPan = active && overflows
  const position = useRef({ left: 0, top: 0 })
  const drag = useRef<Drag | null>(null)
  const touchOrigin = useRef<{ x: number; y: number } | null>(null)
  const moved = useRef(false)

  useLayoutEffect(() => {
    const el = viewport.current
    if (!active || !el || !plane.current) {
      setOverflows(false)
      return
    }
    const map = plane.current
    let gutterX = 0
    let gutterY = 0
    const measure = () => {
      const mobile = matchMedia("(max-width: 700px) and (pointer: coarse)").matches
      const x = mobile ? 0 : Math.round(el.clientWidth / 2)
      const y = mobile ? 0 : Math.round(el.clientHeight / 2)
      const originChanged = x !== gutterX || y !== gutterY
      gutterX = x
      gutterY = y
      el.style.setProperty("--pan-x", x + "px")
      el.style.setProperty("--pan-y", y + "px")
      if (originChanged) el.scrollTo(x + position.current.left, y + position.current.top)
      setOverflows(el.scrollWidth > el.clientWidth || el.scrollHeight > el.clientHeight)
    }
    measure()
    el.scrollTo(gutterX + position.current.left, gutterY + position.current.top)
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    observer.observe(map)
    const remember = () => {
      position.current = { left: el.scrollLeft - gutterX, top: el.scrollTop - gutterY }
    }
    el.addEventListener("scroll", remember)
    return () => {
      observer.disconnect()
      el.removeEventListener("scroll", remember)
      touchOrigin.current = null
      drag.current = null
      moved.current = false
      delete el.dataset.dragging
      onExploreChange(false)
    }
  }, [active, onExploreChange])

  function start(e: PointerEvent<HTMLDivElement>) {
    if (!active || !e.isPrimary || e.button !== 0) return
    moved.current = false
    if (e.pointerType === "touch" && matchMedia("(max-width: 700px) and (pointer: coarse)").matches) return
    const target = e.target as Element
    if (target.closest("button,input,textarea,select")) return
    const el = e.currentTarget
    drag.current = {
      id: e.pointerId,
      x: e.clientX,
      y: e.clientY,
      left: el.scrollLeft,
      top: el.scrollTop,
    }
    target.setPointerCapture(e.pointerId)
  }

  function move(e: PointerEvent<HTMLDivElement>) {
    const origin = drag.current
    if (!origin || origin.id !== e.pointerId) return
    const dx = e.clientX - origin.x
    const dy = e.clientY - origin.y
    if (!moved.current && Math.hypot(dx, dy) < 5) return
    beginDrag(e.currentTarget)
    e.currentTarget.scrollTo(origin.left - dx, origin.top - dy)
  }

  function beginDrag(el: HTMLDivElement) {
    if (moved.current) return
    moved.current = true
    el.dataset.dragging = "true"
    onExploreChange(true)
  }

  function touchStart(e: TouchEvent<HTMLDivElement>) {
    if (!active || !matchMedia("(max-width: 700px) and (pointer: coarse)").matches) return
    if (e.touches.length !== 1) return touchFinish(e)
    if ((e.target as Element).closest("button,input,textarea,select")) return
    touchOrigin.current = { x: e.touches[0].clientX, y: e.touches[0].clientY }
  }

  function touchMove(e: TouchEvent<HTMLDivElement>) {
    const origin = touchOrigin.current
    if (!origin) return
    const finger = e.touches[0]
    if (Math.hypot(finger.clientX - origin.x, finger.clientY - origin.y) >= 5)
      beginDrag(e.currentTarget)
  }

  function touchFinish(e: TouchEvent<HTMLDivElement>) {
    if (!touchOrigin.current) return
    touchOrigin.current = null
    delete e.currentTarget.dataset.dragging
  }

  function finish(e: PointerEvent<HTMLDivElement>) {
    if (drag.current?.id !== e.pointerId) return
    drag.current = null
    delete e.currentTarget.dataset.dragging
  }

  return (
    <>
      <div
        ref={viewport}
        className={active ? "map-viewport" : undefined}
        role={active ? "region" : undefined}
        aria-label={active ? "Agent map" : undefined}
        data-pannable={canPan || undefined}
        tabIndex={canPan ? 0 : undefined}
        onTouchStart={touchStart}
        onTouchMove={touchMove}
        onTouchEnd={touchFinish}
        onTouchCancel={touchFinish}
        onPointerDown={start}
        onPointerMove={move}
        onPointerUp={finish}
        onPointerCancel={finish}
        onLostPointerCapture={finish}
        onDragStart={(e) => {
          if (active) e.preventDefault()
        }}
        onClickCapture={(e) => {
          if (active && moved.current && e.detail > 0) {
            e.preventDefault()
            e.stopPropagation()
          }
        }}
      >
        <div className={active ? "map-travel" : undefined}>
          <div ref={plane} className={active ? "map-plane" : undefined}>
            {children}
          </div>
        </div>
      </div>
      {canPan && (
        <p className="map-hint"><span className="mouse-hint">click and drag</span><span className="touch-hint">swipe</span> to explore your agents</p>
      )}
    </>
  )
}
