import { computed, ref, shallowRef } from 'vue'
import { defineStore } from 'pinia'
import { ApiError, api, isValidReport } from '@/api/api'
import { mapBus } from '@/map/bus'
import {
  CATEGORY_COLORS,
  CATEGORY_NAMES,
  type AuditReport,
  type BlindspotPolygon,
  type CategoryKey,
  type Isochrone,
  type LatLng,
  type MinuteKey,
  type PoiDatum,
  type SeedCommunity
} from '@/types/report'

/** 默认中心点：上海市浦东新区海科路1650号（张江），与离线 mock 数据一致 */
export const DEFAULT_CENTER: LatLng = { lat: 31.2045, lng: 121.599 }

/** 内置快捷社区：后端 /api/seed/communities 不可用时的兜底 */
const BUILTIN_COMMUNITIES: SeedCommunity[] = [
  { name: '张江·海科路1650号', lat: 31.2045, lng: 121.599 },
  { name: '张江·香楠小区', lat: 31.1993, lng: 121.6108 },
  { name: '张江·丹桂路', lat: 31.2124, lng: 121.5883 },
  { name: '陆家嘴·世纪大道', lat: 31.2341, lng: 121.5055 }
]

export type DataSource = 'api' | 'mock'
export type BarMode = 'count' | 'density' | 'walk'

export const useAuditStore = defineStore('audit', () => {
  /* ---------------- state ---------------- */
  const report = shallowRef<AuditReport | null>(null)
  const loading = ref(false)
  const backendOnline = ref<boolean | null>(null)
  const dataSource = ref<DataSource | null>(null)
  const errorMessage = ref<string>('')
  const mockReason = ref<string>('')
  const addressInput = ref('上海市浦东新区海科路1650号')
  /** 地图上待确认的中心点（点选 / 拖拽产生，尚未发起体检） */
  const pending = ref<LatLng | null>(null)
  const bands = ref<MinuteKey[]>(['5', '10', '15'])
  const layers = ref({ pois: true, blindspots: true, mask: true })
  const barMode = ref<BarMode>('count')
  const selectedBlindspot = ref<number | null>(null)
  const selectedPoi = ref<PoiDatum | null>(null)
  const communities = ref<SeedCommunity[]>(BUILTIN_COMMUNITIES)
  /** 窄屏（<1280px）下左侧分析面板的抽屉开关 */
  const sideOpen = ref(false)

  /* ---------------- getters ---------------- */
  const center = computed<LatLng>(() => report.value?.meta.center ?? DEFAULT_CENTER)
  const activeTimes = computed(() => bands.value.slice().sort((a, b) => Number(a) - Number(b)))
  const isochrones = computed<Record<string, Isochrone>>(() => report.value?.isochrones ?? {})
  const categories = computed(() => report.value?.facilities.categories ?? [])
  const funnel = computed(() => report.value?.facilities.cleaning_funnel ?? null)
  const blindspots = computed<BlindspotPolygon[]>(() => report.value?.blindspots.polygons ?? [])
  const totalBlindspotArea = computed(() => report.value?.blindspots.total_area_m2 ?? 0)
  const area15 = computed(() => report.value?.isochrones['15']?.area_m2 ?? 0)
  const allPois = computed<PoiDatum[]>(() => {
    const list: PoiDatum[] = []
    for (const c of categories.value) {
      for (const p of c.pois) {
        list.push({
          ...p,
          category: c.key,
          categoryName: CATEGORY_NAMES[c.key as CategoryKey] ?? c.name,
          color: CATEGORY_COLORS[c.key as CategoryKey] ?? '#8ea3c4'
        })
      }
    }
    return list
  })
  const sourceLabel = computed(() => {
    if (dataSource.value === 'api') return '实时后端'
    if (dataSource.value === 'mock') return '离线 mock'
    return '未加载'
  })
  const isDegraded = computed(() => report.value?.meta.degraded === true || dataSource.value === 'mock')

  /* ---------------- actions ---------------- */
  function applyReport(r: AuditReport) {
    report.value = r
    addressInput.value = r.meta.address || addressInput.value
    const c = r.meta.center
    if (c && typeof c.lat === 'number') {
      // 后端返回的中心点优先
      pending.value = null
    }
  }

  async function loadMock(reason: string) {
    const mock = await api.mockReport()
    if (!isValidReport(mock)) throw new ApiError('离线 mock 数据结构不符合契约 §4', 'MOCK_INVALID')
    applyReport(mock)
    dataSource.value = 'mock'
    mockReason.value = reason
    return mock
  }

  /** 先探活，再决定首屏走 live 还是直接回退离线数据 */
  async function bootstrap() {
    loading.value = true
    errorMessage.value = ''
    // 首屏默认点必须跟随「后端当前激活的社区」。
    // 原先硬编码张江（DEFAULT_CENTER），一旦后端切到别的社区快照，
    // 首屏就必然 SEED_MISSING —— replay 只覆盖已采集快照的社区，
    // 请求范围外的坐标会被后端明确拒绝（不再静默替换）。
    let start: { lat: number; lng: number } = { lat: DEFAULT_CENTER.lat, lng: DEFAULT_CENTER.lng }
    let activeCommunity: string | null = null
    try {
      const h = await api.health()
      backendOnline.value = h.status === 'ok'
      activeCommunity = h.seed_community ?? null
      if (h.seed_center && typeof h.seed_center.lat === 'number' && typeof h.seed_center.lng === 'number') {
        start = { lat: h.seed_center.lat, lng: h.seed_center.lng }
      }
    } catch {
      backendOnline.value = false
    }
    try {
      if (backendOnline.value) {
        const list = await api.seedCommunities().catch(() => null)
        if (Array.isArray(list) && list.length) {
          communities.value = list
          // 地址框默认值跟随「后端当前激活的社区」。
          // 原先硬编码张江，切到别的社区后一点「分析」就会 geocode 失败
          // （replay 只覆盖已采集快照的社区）→ 整个报告回退到前端内置 mock。
          const active = list.find((c) => c.id === activeCommunity) ?? list[0]
          if (active?.address) addressInput.value = active.address
        }
      }
    } catch {
      /* 忽略：快捷社区不是关键路径 */
    }
    await runAudit(start)
    loading.value = false
  }

  /** 核心动作：请求 /api/audit；失败时自动回退到 offline mock，保证前端可独立运行 */
  async function runAudit(target?: { lat?: number; lng?: number; address?: string }) {
    loading.value = true
    errorMessage.value = ''
    const params = target ?? { lat: center.value.lat, lng: center.value.lng }
    try {
      const r = await api.audit(params)
      if (!isValidReport(r)) throw new ApiError('后端返回结构不符合契约 §4', 'CONTRACT_MISMATCH')
      applyReport(r)
      dataSource.value = 'api'
      mockReason.value = ''
      backendOnline.value = true
      selectedBlindspot.value = null
      selectedPoi.value = null
      mapBus.emit({ type: 'fly-to', ...r.meta.center, zoom: 15 })
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      errorMessage.value = msg
      if (e instanceof ApiError && (e.code === 'NETWORK' || e.code === 'TIMEOUT')) backendOnline.value = false
      try {
        const mock = await loadMock(msg)
        selectedBlindspot.value = null
        selectedPoi.value = null
        mapBus.emit({ type: 'fly-to', ...mock.meta.center, zoom: 15 })
      } catch (e2) {
        errorMessage.value = msg + ' ｜ 离线数据同样不可用：' + (e2 instanceof Error ? e2.message : String(e2))
      }
    } finally {
      loading.value = false
    }
  }

  /** ① 地址输入 → /api/geocode → /api/audit */
  async function geocodeAndRun(address: string) {
    const q = address.trim()
    if (!q) return
    loading.value = true
    errorMessage.value = ''
    try {
      const g = await api.geocode(q)
      pending.value = null
      addressInput.value = g.address || q
      await runAudit({ lat: g.lat, lng: g.lng })
      mapBus.emit({ type: 'fly-to', lat: g.lat, lng: g.lng, zoom: 15 })
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      errorMessage.value = '地理编码失败：' + msg
      // 逐字兜底：直接让后端用地址体检（后端内部也会做地理编码）
      await runAudit({ address: q })
    } finally {
      loading.value = false
    }
  }

  /** ② 地图点选 / ③ 拖拽 marker：先落一个待确认点，再确认体检 */
  function setPending(lat: number, lng: number) {
    pending.value = { lat, lng }
  }

  async function confirmPending() {
    const p = pending.value
    if (!p) return
    await runAudit({ lat: p.lat, lng: p.lng })
  }

  function toggleBand(m: MinuteKey) {
    const i = bands.value.indexOf(m)
    if (i >= 0) {
      if (bands.value.length === 1) return // 至少保留一档
      bands.value.splice(i, 1)
    } else {
      bands.value.push(m)
    }
  }

  function selectBlindspot(i: number | null) {
    selectedBlindspot.value = i
    selectedPoi.value = null
    if (i === null) {
      mapBus.emit({ type: 'clear-highlight' })
      return
    }
    const b = blindspots.value[i]
    if (b) mapBus.emit({ type: 'focus-blindspot', index: i, lat: b.sample_point.lat, lng: b.sample_point.lng })
  }

  function selectPoi(p: PoiDatum | null) {
    selectedPoi.value = p
    if (p) {
      selectedBlindspot.value = null
      mapBus.emit({ type: 'focus-poi', lat: p.lat, lng: p.lng })
    }
  }

  function resetView() {
    selectedBlindspot.value = null
    selectedPoi.value = null
    mapBus.emit({ type: 'reset-view' })
  }

  return {
    // state
    report, loading, backendOnline, dataSource, errorMessage, mockReason, addressInput,
    pending, bands, layers, barMode, selectedBlindspot, selectedPoi, communities, sideOpen,
    // getters
    center, activeTimes, isochrones, categories, funnel, blindspots, totalBlindspotArea,
    area15, allPois, sourceLabel, isDegraded,
    // actions
    bootstrap, runAudit, geocodeAndRun, setPending, confirmPending, toggleBand,
    selectBlindspot, selectPoi, resetView
  }
})
