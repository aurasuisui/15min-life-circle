import type { PoiDatum } from '@/types/report'

/**
 * POI 点位图层：**单个 canvas 自绘**（WebGL 底图之上的 2D 叠加层），
 * 而不是为每个 POI 建一个 DOM Marker —— 满足「不要用大量 DOM Marker」的要求，
 * 任意数量 POI 都只占 1 个 DOM 节点，缩放/平移不产生 DOM 抖动。
 */
export interface PoiCanvasLayer {
  render(): void
  hitTest(lng: number, lat: number, tolPx: number): PoiDatum | null
  setVisible(v: boolean): void
  destroy(): void
}

interface LayerOptions {
  getPois: () => PoiDatum[]
  onClickPoi?: (poi: PoiDatum) => void
}

export function createPoiCanvasLayer(map: any, opts: LayerOptions): PoiCanvasLayer | null {
  const PointCtor = (window as unknown as { BMapGL?: any }).BMapGL?.Point
  // 4.0 同时提供 pointToPixel 与 pointToOverlayPixel；任一可用即可
  const projectFn: ((p: any) => { x: number; y: number }) | null =
    typeof map.pointToPixel === 'function'
      ? (p: any) => map.pointToPixel(p)
      : typeof map.pointToOverlayPixel === 'function'
        ? (p: any) => map.pointToOverlayPixel(p)
        : null
  if (!projectFn || !PointCtor) return null
  const project: (p: any) => { x: number; y: number } = projectFn

  const container: HTMLElement = map.getContainer()
  const canvas = document.createElement('canvas')
  canvas.className = 'poi-canvas'
  canvas.style.cssText =
    'position:absolute;left:0;top:0;pointer-events:none;z-index:5;transition:opacity .15s linear'
  container.appendChild(canvas)
  const ctx2d = canvas.getContext('2d')
  if (!ctx2d) {
    canvas.remove()
    return null
  }
  const ctx: CanvasRenderingContext2D = ctx2d

  let visible = true
  let lastPois: { p: PoiDatum; x: number; y: number }[] = []

  function resize() {
    const size = map.getSize()
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    canvas.width = Math.max(1, Math.round(size.width * dpr))
    canvas.height = Math.max(1, Math.round(size.height * dpr))
    canvas.style.width = size.width + 'px'
    canvas.style.height = size.height + 'px'
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  }

  function render() {
    resize()
    const w = canvas.width / Math.min(window.devicePixelRatio || 1, 2)
    const h = canvas.height / Math.min(window.devicePixelRatio || 1, 2)
    ctx.clearRect(0, 0, w, h)
    if (!visible) {
      lastPois = []
      return
    }
    const pois = opts.getPois()
    const zoom = map.getZoom()
    const showLabel = zoom >= 16
    const drawn: { p: PoiDatum; x: number; y: number }[] = []
    const pad = 24

    for (const p of pois) {
      let xy: { x: number; y: number }
      try {
        xy = project(new PointCtor(p.lng, p.lat))
      } catch {
        continue
      }
      if (!xy || !Number.isFinite(xy.x)) continue
      if (xy.x < -pad || xy.y < -pad || xy.x > w + pad || xy.y > h + pad) continue
      drawn.push({ p, x: xy.x, y: xy.y })
    }

    // 先画光晕，再画实心点，视觉上更接近热力叠加
    ctx.save()
    for (const { p, x, y } of drawn) {
      ctx.globalAlpha = 0.22
      ctx.fillStyle = p.color
      ctx.beginPath()
      ctx.arc(x, y, 9, 0, Math.PI * 2)
      ctx.fill()
    }
    ctx.restore()

    for (const { p, x, y } of drawn) {
      ctx.beginPath()
      ctx.arc(x, y, 4.6, 0, Math.PI * 2)
      ctx.fillStyle = p.color
      ctx.fill()
      ctx.lineWidth = 1.4
      ctx.strokeStyle = 'rgba(8,16,30,0.85)'
      ctx.stroke()
    }

    if (showLabel) {
      ctx.font = '600 11px system-ui, "Microsoft YaHei", sans-serif'
      ctx.textBaseline = 'middle'
      for (const { p, x, y } of drawn) {
        const label = p.name
        const tw = ctx.measureText(label).width
        ctx.fillStyle = 'rgba(8,14,26,0.72)'
        ctx.beginPath()
        ctx.roundRect(x + 8, y - 8, tw + 10, 16, 4)
        ctx.fill()
        ctx.fillStyle = 'rgba(233,241,255,0.94)'
        ctx.fillText(label, x + 13, y + 0.5)
      }
    }
    lastPois = drawn
  }

  function hitTest(lng: number, lat: number, tolPx: number): PoiDatum | null {
    let xy: { x: number; y: number }
    try {
      xy = project(new PointCtor(lng, lat))
    } catch {
      return null
    }
    let best: PoiDatum | null = null
    let bestD = tolPx
    for (const { p, x, y } of lastPois) {
      const d = Math.hypot(x - xy.x, y - xy.y)
      if (d <= bestD) {
        bestD = d
        best = p
      }
    }
    return best
  }

  const onMoveStart = () => {
    canvas.style.opacity = '0'
  }
  const onMoveEnd = () => {
    canvas.style.opacity = '1'
    render()
  }
  map.addEventListener('movestart', onMoveStart)
  map.addEventListener('zoomstart', onMoveStart)
  map.addEventListener('moveend', onMoveEnd)
  map.addEventListener('zoomend', onMoveEnd)
  map.addEventListener('resize', onMoveEnd)
  render()

  return {
    render,
    hitTest,
    setVisible(v: boolean) {
      visible = v
      canvas.style.display = v ? 'block' : 'none'
      render()
    },
    destroy() {
      map.removeEventListener('movestart', onMoveStart)
      map.removeEventListener('zoomstart', onMoveStart)
      map.removeEventListener('moveend', onMoveEnd)
      map.removeEventListener('zoomend', onMoveEnd)
      map.removeEventListener('resize', onMoveEnd)
      canvas.remove()
    }
  }
}
