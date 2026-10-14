<script setup lang="ts">
/**
 * 地图主视图 · 百度地图 JSAPI 4.0（window.BMapGL 命名空间）
 *
 * 图层构成：
 *   1. AreaMask            15 分钟圈外整体压暗（突出可达范围）
 *   2. Polygon × 3         5/10/15 分钟等时圈（色阶填充 + 图例，不依赖 BMapGLLib 热力图）
 *   3. Polygon × N         盲区「灰色区域」（红色半透明，可点击定位）
 *   4. 单个 canvas 自绘层   POI 点位（按类目着色，非 DOM Marker）
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useAuditStore } from '@/stores/audit'
import { mapBus } from '@/map/bus'
import { CENTER_MARKER_SVG, geometryToPaths, gl, loadBaiduMap } from '@/map/baidu'
import { createPoiCanvasLayer, type PoiCanvasLayer } from '@/map/poi-layer'
import { MINUTE_KEYS, type MinuteKey } from '@/types/report'

const store = useAuditStore()
const el = ref<HTMLDivElement | null>(null)
const mapError = ref('')
const mapReady = ref(false)
const maskMode = ref<'focus-mask' | 'rect-fallback' | 'off'>('off')
const poiMode = ref<'canvas' | 'circle' | 'off'>('off')

let map: any = null
let marker: any = null
let focusMask: any = null
let poiLayer: PoiCanvasLayer | null = null
let overlays: any[] = []
let poiCircles: any[] = []
let lastOverlayClick = 0
let offBus: (() => void) | null = null

const BAND_STYLE: Record<MinuteKey, { color: string; fill: number; label: string }> = {
  '5': { color: '#31d0a5', fill: 0.26, label: '5 分钟' },
  '10': { color: '#4ee0ff', fill: 0.17, label: '10 分钟' },
  '15': { color: '#5aa9ff', fill: 0.1, label: '15 分钟' }
}

const center = computed(() => store.pending ?? store.center)
const markers = computed(() => (store.pending ? '待确认中心点' : '分析中心点'))

/* ---------------- 初始化 ---------------- */
onMounted(async () => {
  try {
    await loadBaiduMap()
  } catch (e) {
    mapError.value = e instanceof Error ? e.message : String(e)
    return
  }
  try {
    initMap()
    mapReady.value = true
    renderAll()
  } catch (e) {
    mapError.value = '地图初始化失败：' + (e instanceof Error ? e.message : String(e))
  }
  offBus = mapBus.on(handleCommand)
})

function initMap() {
  const G = gl()
  map = new G.Map(el.value)
  map.centerAndZoom(new G.Point(center.value.lng, center.value.lat), 15)
  map.enableScrollWheelZoom(true)
  // 可选控件：任一不可用都不应影响主流程
  for (const make of [
    () => new G.ScaleControl(),
    () => new G.NavigationControl3D({ anchor: 'BMAP_ANCHOR_BOTTOM_RIGHT' })
  ]) {
    try {
      const c = make()
      if (c) map.addControl(c)
    } catch {
      /* 忽略 */
    }
  }

  map.addEventListener('click', (e: any) => {
    if (Date.now() - lastOverlayClick < 300) return
    const ll = e?.latLng
    if (!ll) return
    // 命中 POI 则选中该 POI，否则落一个待确认中心点
    const hit = poiLayer?.hitTest(ll.lng, ll.lat, 11) ?? null
    if (hit) {
      store.selectPoi(hit)
      return
    }
    store.setPending(ll.lat, ll.lng)
  })

  map.addEventListener('mousemove', (e: any) => {
    const ll = e?.latLng
    if (!ll || !poiLayer || !map?.getContainer()) return
    const hit = poiLayer.hitTest(ll.lng, ll.lat, 9)
    map.getContainer().style.cursor = hit ? 'pointer' : ''
  })

  poiLayer = createPoiCanvasLayer(map, { getPois: () => (store.layers.pois ? store.allPois : []) })
  if (!poiLayer) poiMode.value = 'circle'

  // 便于自测（scripts/smoke.mjs）与手动调试：开发模式，或显式带 ?debug=1 时，
  // 把地图实例挂到 window。生产默认不暴露。
  if (import.meta.env.DEV || location.search.includes('debug=1')) {
    (window as unknown as Record<string, unknown>).__bmap = map
  }
}

/* ---------------- 渲染 ---------------- */
function clearAll() {
  if (focusMask) {
    try {
      map.removeFocusMask(focusMask)
    } catch {
      /* 忽略 */
    }
    focusMask = null
  }
  overlays.forEach((o) => map.removeOverlay(o))
  overlays = []
  poiCircles.forEach((o) => map.removeOverlay(o))
  poiCircles = []
  if (marker) {
    try {
      map.removeOverlay(marker)
    } catch {
      /* 忽略 */
    }
    marker = null
  }
}

function renderAll() {
  if (!map) return
  clearAll()
  const G = gl()
  const report = store.report
  if (!report) return

  /* 1) 圈外压暗 */
  addAreaMask(G)

  /* 2) 等时圈（从大到小，保证小圈叠在大圈之上） */
  for (const m of [...MINUTE_KEYS].sort((a, b) => Number(b) - Number(a))) {
    if (!store.bands.includes(m)) continue
    const iso = report.isochrones[m]
    if (!iso) continue
    const st = BAND_STYLE[m]
    try {
      const poly = new G.Polygon(geometryToPaths(iso.geometry, G.Point), {
        strokeColor: st.color,
        strokeWeight: m === '15' ? 2.4 : 2,
        strokeOpacity: 0.95,
        fillColor: st.color,
        fillOpacity: st.fill
      })
      map.addOverlay(poly)
      overlays.push(poly)
    } catch {
      /* 单个几何失败不应中断整张图 */
    }
  }

  /* 3) 盲区 */
  if (store.layers.blindspots) {
    report.blindspots.polygons.forEach((b, i) => {
      try {
        const selected = store.selectedBlindspot === i
        const poly = new G.Polygon(geometryToPaths(b.geometry, G.Point), {
          strokeColor: '#ff5d73',
          strokeWeight: selected ? 3.4 : 2,
          strokeOpacity: 0.95,
          strokeStyle: 'dashed',
          fillColor: '#ff4d6d',
          fillOpacity: selected ? 0.5 : 0.32
        })
        poly.addEventListener('click', () => {
          lastOverlayClick = Date.now()
          store.selectBlindspot(i)
        })
        poly.addEventListener('mouseover', () => poly.setFillOpacity(0.48))
        poly.addEventListener('mouseout', () => poly.setFillOpacity(store.selectedBlindspot === i ? 0.5 : 0.32))
        map.addOverlay(poly)
        overlays.push(poly)
      } catch {
        /* 忽略 */
      }
    })
  }

  /* 4) POI */
  renderPois()

  /* 5) 中心点标记 */
  addCenterMarker(G)
}

/** 15 分钟圈外整体压暗：优先 BMapGL.AreaMask，失败则退化为外接矩形外的 4 块矩形 */
function addAreaMask(G: Record<string, any>) {
  maskMode.value = 'off'
  if (!store.layers.mask) return
  const iso = store.report?.isochrones['15']
  if (!iso) return
  let ring: any[]
  try {
    ring = geometryToPaths(iso.geometry, G.Point)[0]
  } catch {
    return
  }
  if (!ring?.length) return

  // JSAPI 4.0 的 API 是 BMapGL.MapMask + map.addFocusMask()。
  // （GL 时代的 BMapGL.AreaMask 在 4.0 中已不存在——实测 typeof BMapGL.AreaMask === 'undefined'。）
  if (typeof G.MapMask === 'function' && typeof map.addFocusMask === 'function') {
    for (const build of [
      () => new G.MapMask(ring, { fillColor: '#050a14', fillOpacity: 0.66, strokeColor: 'rgba(0,0,0,0)', strokeWeight: 1 }),
      () => new G.MapMask({ path: ring, fillColor: '#050a14', fillOpacity: 0.66 })
    ]) {
      try {
        const mask = build()
        map.addFocusMask(mask)
        focusMask = mask
        maskMode.value = 'focus-mask'
        return
      } catch {
        /* 换一种构造形态再试 */
      }
    }
  }

  // 兜底：绕等时圈外接矩形铺 4 块矩形
  try {
    const lngs = ring.map((p: any) => p.lng)
    const lats = ring.map((p: any) => p.lat)
    const pad = 0.004
    const [w, e] = [Math.min(...lngs) - pad, Math.max(...lngs) + pad]
    const [s, n] = [Math.min(...lats) - pad, Math.max(...lats) + pad]
    const rects: number[][][] = [
      [[-180, -85], [180, -85], [180, s], [-180, s]],
      [[-180, n], [180, n], [180, 85], [-180, 85]],
      [[-180, s], [w, s], [w, n], [-180, n]],
      [[e, s], [180, s], [180, n], [e, n]]
    ]
    for (const r of rects) {
      const poly = new G.Polygon(
        r.map(([lng, lat]) => new G.Point(lng, lat)),
        { strokeColor: 'rgba(0,0,0,0)', strokeWeight: 0, fillColor: '#050a14', fillOpacity: 0.62 }
      )
      map.addOverlay(poly)
      overlays.push(poly)
    }
    maskMode.value = 'rect-fallback'
  } catch {
    maskMode.value = 'off'
  }
}

/** POI：canvas 自绘优先；投影 API 不可用时退化为 GL 矢量圆（仍非 DOM Marker） */
function renderPois() {
  if (poiLayer) {
    poiLayer.setVisible(store.layers.pois)
    poiLayer.render()
    poiMode.value = store.layers.pois ? 'canvas' : 'off'
    return
  }
  const G = gl()
  if (typeof G.Circle !== 'function') {
    poiMode.value = 'off'
    return
  }
  poiCircles.forEach((o) => map.removeOverlay(o))
  poiCircles = []
  if (!store.layers.pois) {
    poiMode.value = 'off'
    return
  }
  for (const p of store.allPois) {
    try {
      const c = new G.Circle(new G.Point(p.lng, p.lat), 16, {
        strokeColor: p.color,
        strokeWeight: 1.2,
        strokeOpacity: 0.9,
        fillColor: p.color,
        fillOpacity: 0.85
      })
      c.addEventListener('click', () => {
        lastOverlayClick = Date.now()
        store.selectPoi(p)
      })
      map.addOverlay(c)
      poiCircles.push(c)
    } catch {
      /* 忽略 */
    }
  }
  poiMode.value = 'circle'
}

function addCenterMarker(G: Record<string, any>) {
  // 先摘掉旧标记，避免拖拽/点选时不断叠加
  if (marker) {
    try {
      map.removeOverlay(marker)
    } catch {
      /* 忽略 */
    }
    marker = null
  }
  const c = center.value
  const point = new G.Point(c.lng, c.lat)
  let opts: Record<string, unknown> = { enableDragging: true }
  try {
    if (typeof G.Icon === 'function' && typeof G.Size === 'function') {
      opts = {
        ...opts,
        icon: new G.Icon(CENTER_MARKER_SVG, new G.Size(44, 44), {
          anchor: new G.Size(22, 22),
          imageSize: new G.Size(44, 44)
        })
      }
    }
  } catch {
    /* 用默认图标 */
  }
  try {
    marker = new G.Marker(point, opts)
  } catch {
    marker = new G.Marker(point)
  }
  marker.addEventListener('dragend', (e: any) => {
    const ll = e?.latLng ?? e?.point
    if (ll) store.setPending(ll.lat, ll.lng)
  })
  map.addOverlay(marker) // 单独管理：不放进 overlays，避免重复添加
}

/* ---------------- 地图指令 ---------------- */
function flyTo(lat: number, lng: number, zoom?: number) {
  if (!map) return
  const G = gl()
  const p = new G.Point(lng, lat)
  try {
    if (typeof map.flyTo === 'function') {
      map.flyTo(p, zoom ?? Math.max(15, map.getZoom()))
      return
    }
  } catch {
    /* 落到下面的兜底 */
  }
  if (zoom) map.centerAndZoom(p, zoom)
  else map.panTo(p)
}

function handleCommand(cmd: Parameters<Parameters<typeof mapBus.on>[0]>[0]) {
  if (!map) return
  if (cmd.type === 'fly-to' || cmd.type === 'focus-poi') flyTo(cmd.lat, cmd.lng, cmd.zoom ?? 16)
  else if (cmd.type === 'focus-blindspot') {
    flyTo(cmd.lat, cmd.lng, 16)
    renderAll()
  } else if (cmd.type === 'reset-view') {
    const c = store.center
    const G = gl()
    map.centerAndZoom(new G.Point(c.lng, c.lat), 15)
    renderAll()
  } else if (cmd.type === 'clear-highlight') {
    renderAll()
  }
}

/* ---------------- 响应式 ---------------- */
watch(() => store.report, () => renderAll())
watch(() => store.layers, () => renderAll(), { deep: true })
watch(() => store.bands, () => renderAll(), { deep: true })
watch(() => store.selectedBlindspot, () => renderAll())
watch(
  () => store.pending,
  () => {
    if (!map) return
    addCenterMarker(gl()) // 只更新标记位置
  },
  { deep: true }
)

onBeforeUnmount(() => {
  offBus?.()
  poiLayer?.destroy()
  poiLayer = null
  try {
    map?.clearOverlays()
  } catch {
    /* 忽略 */
  }
  map = null
})

const poiWalk = computed(() => {
  const p = store.selectedPoi
  if (!p) return ''
  const s = p.walk_seconds
  return s > 0 ? Math.round(s / 60) + ' 分钟（' + s + ' s）' : '—'
})
function closePoi() {
  store.selectedPoi = null
}
</script>

<template>
  <section class="map-stage">
    <div id="bmap-container" ref="el" />

    <!-- 顶部信息 -->
    <div class="map-overlay map-title">
      <div class="map-panel">
        <div class="map-addr">
          <span class="dotmark" />
          <span>{{ store.report?.meta.address || '上海市浦东新区海科路1650号' }}</span>
        </div>
        <div class="map-sub">
          {{ center.lat.toFixed(6) }}, {{ center.lng.toFixed(6) }} · BD09 · {{ markers }}
        </div>
      </div>
      <div class="map-panel" style="padding: 7px 9px; display: flex; gap: 6px; flex-wrap: wrap; align-items: center">
        <span style="font-size: 10.5px; color: var(--muted); letter-spacing: 0.5px">时间档位</span>
        <button
          v-for="m in MINUTE_KEYS"
          :key="m"
          class="chip"
          :class="{ on: store.bands.includes(m) }"
          @click="store.toggleBand(m)"
        >
          <span class="dot" :style="{ background: BAND_STYLE[m].color }" />{{ BAND_STYLE[m].label }}
        </button>
      </div>
    </div>

    <!-- 右上工具 -->
    <div class="map-overlay map-tools">
      <div class="map-panel maptool-row">
        <button class="btn sm ghost" @click="store.resetView()">复位</button>
        <button class="btn sm ghost" :disabled="store.loading" @click="store.runAudit()">
          <span v-if="store.loading" class="spin" />重新体检
        </button>
      </div>
      <div class="map-panel" style="padding: 6px 9px; font-size: 10.5px; color: var(--muted); line-height: 1.7">
        <div>POI 图层：{{ poiMode === 'canvas' ? 'canvas 自绘' : poiMode === 'circle' ? 'GL 矢量圆' : '已关闭' }}</div>
        <div>圈外压暗：{{ maskMode === 'focus-mask' ? 'MapMask 掩膜' : maskMode === 'rect-fallback' ? '矩形兜底' : '已关闭' }}</div>
      </div>
    </div>

    <!-- 图例 -->
    <div class="map-overlay map-legend">
      <div class="map-panel">
        <div class="legend-title">步行等时圈</div>
        <div v-for="m in [...MINUTE_KEYS].reverse()" :key="m" class="legend-row">
          <span class="legend-swatch" :style="{ background: BAND_STYLE[m].color + '55', borderColor: BAND_STYLE[m].color }" />
          <span>{{ BAND_STYLE[m].label }}</span>
        </div>
        <div class="legend-row">
          <span class="legend-swatch" style="background: rgba(255,77,109,0.4); border-color: #ff8fa3" />
          <span>服务盲区</span>
        </div>
        <div class="legend-row">
          <span class="legend-swatch" style="background: rgba(5,10,20,0.66); border-color: rgba(255,255,255,0.2)" />
          <span>圈外不可达（压暗）</span>
        </div>
      </div>
    </div>

    <!-- 待确认中心点 -->
    <div v-if="store.pending" class="map-overlay pending-bar">
      <div class="map-panel" style="display: flex; align-items: center; gap: 10px">
        <span style="font-size: 11.5px; color: var(--text-2)">
          已选点 {{ store.pending.lat.toFixed(5) }}, {{ store.pending.lng.toFixed(5) }}
        </span>
        <button class="btn primary sm" :disabled="store.loading" @click="store.confirmPending()">在此处体检</button>
        <button class="btn ghost sm" @click="store.pending = null">取消</button>
      </div>
    </div>

    <!-- POI 详情 -->
    <div v-if="store.selectedPoi" class="map-overlay poi-card">
      <div class="map-panel">
        <h4><span class="swatch" :style="{ background: store.selectedPoi.color }" />{{ store.selectedPoi.name }}</h4>
        <div class="row"><span>类目</span><b>{{ store.selectedPoi.categoryName }}</b></div>
        <div class="row"><span>步行耗时</span><b>{{ poiWalk }}</b></div>
        <div class="row"><span>地址</span><b style="max-width: 150px; text-align: right">{{ store.selectedPoi.address || '—' }}</b></div>
        <div class="row"><span>坐标</span><b>{{ store.selectedPoi.lat.toFixed(5) }}, {{ store.selectedPoi.lng.toFixed(5) }}</b></div>
        <div style="display: flex; justify-content: flex-end; margin-top: 7px">
          <button class="btn sm ghost" @click="closePoi()">关闭</button>
        </div>
      </div>
    </div>

    <!-- 加载 / 错误 -->
    <div v-if="!mapReady && !mapError" class="map-loading">
      <div class="box"><span class="spin" /><span>正在加载百度地图 JSAPI 4.0…</span></div>
    </div>
    <div v-if="mapError" class="map-fallback">
      <div class="inner">
        <h4>地图未能加载</h4>
        <p>{{ mapError }}</p>
        <p style="color: var(--muted)">
          排查顺序：① <code>frontend/.env.local</code> 是否配置了 <code>VITE_BAIDU_MAP_BROWSER_AK</code>（浏览器端 AK，
          不能用服务端 AK）；② 该 AK 的 <strong>Referer 白名单</strong>是否包含当前地址（本机开发为
          <code>localhost</code> / <code>127.0.0.1</code>）；③ 网络是否可达 <code>api.map.baidu.com</code>。
        </p>
        <p style="color: var(--muted)">报告面板与图表不依赖地图，仍可正常查看。</p>
      </div>
    </div>
  </section>
</template>
