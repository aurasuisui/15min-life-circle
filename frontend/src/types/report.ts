/**
 * 体检报告类型定义 —— 严格对齐 docs/CONTRACT.md §4（FROZEN）。
 * 字段名一律以契约为准，不得自行改名。
 */

/** 运行数据模式 */
export type DataMode = 'live' | 'replay' | 'mock'
/** 评分等级 */
export type Grade = 'A' | 'B' | 'C' | 'D'
/** 补点建议优先级 */
export type Priority = 'high' | 'medium' | 'low'
/** 时间档位（分钟，字符串键，见 §4 isochrones） */
export type MinuteKey = '5' | '10' | '15'
export const MINUTE_KEYS: MinuteKey[] = ['5', '10', '15']

/** 8 类民生设施机器名（§4.2 冻结） */
export type CategoryKey =
  | 'market'
  | 'pharmacy'
  | 'primary_school'
  | 'clinic'
  | 'supermarket'
  | 'elderly_care'
  | 'bus_stop'
  | 'park'

export const CATEGORY_KEYS: CategoryKey[] = [
  'market',
  'pharmacy',
  'primary_school',
  'clinic',
  'supermarket',
  'elderly_care',
  'bus_stop',
  'park'
]

/** 中文名（§4.2 冻结） */
export const CATEGORY_NAMES: Record<CategoryKey, string> = {
  market: '菜市场',
  pharmacy: '药店',
  primary_school: '小学',
  clinic: '社区医疗',
  supermarket: '超市便利店',
  elderly_care: '养老设施',
  bus_stop: '公交站',
  park: '公园绿地'
}

/** POI 按类目着色的调色板（前端自定义，非契约字段） */
export const CATEGORY_COLORS: Record<CategoryKey, string> = {
  market: '#ff8a3d',
  pharmacy: '#2fd4a7',
  primary_school: '#5aa9ff',
  clinic: '#ff5d73',
  supermarket: '#c58cff',
  elderly_care: '#ffd166',
  bus_stop: '#4ee0ff',
  park: '#7ee787'
}

export interface LatLng {
  lat: number
  lng: number
}

/** 多边形 / 多多边形几何（BD09，坐标顺序 [lng, lat]，多边形必须闭合） */
export interface PolygonGeometry {
  type: 'Polygon'
  coordinates: number[][][]
}
export interface MultiPolygonGeometry {
  type: 'MultiPolygon'
  coordinates: number[][][][]
}
export type Geometry = PolygonGeometry | MultiPolygonGeometry

export interface Quota {
  routes_used: number
  routes_limit: number
  poi_used: number
  poi_limit: number
}

export interface ReportMeta {
  center: LatLng
  address: string
  generated_at: string
  data_mode: DataMode
  degraded: boolean
  degrade_reasons: string[]
  quota: Quota
}

export interface Isochrone {
  target_seconds: number
  area_m2: number
  n_samples: number
  routes_charged: number
  uncertainty_band_ratio: number
  geometry: Geometry
}

/** key 为分钟数字符串：'5' | '10' | '15' */
export type Isochrones = Record<string, Isochrone>

export interface Poi {
  uid: string
  name: string
  lat: number
  lng: number
  address: string
  tag: string
  walk_seconds: number
}

export interface FacilityCategory {
  key: CategoryKey
  name: string
  query: string
  count: number
  density_per_km2: number
  nearest_walk_seconds: number
  /** 0~1 */
  coverage_score: number
  pois: Poi[]
}

export interface CleaningFunnel {
  raw: number
  deduped: number
  corrected: number
  inside: number
}

export interface Facilities {
  categories: FacilityCategory[]
  cleaning_funnel: CleaningFunnel
}

export interface BlindspotPolygon {
  area_m2: number
  /** 缺失的设施类目机器名 */
  missing: string[]
  sample_point: LatLng
  geometry: Geometry
}

export interface Blindspots {
  total_area_m2: number
  polygons: BlindspotPolygon[]
}

export interface ScoreDimension {
  key: string
  name: string
  score: number
  weight: number
  raw_count: number
}

export interface Score {
  /** 0~100 */
  total: number
  grade: Grade
  dimensions: ScoreDimension[]
}

export interface Siting {
  lat: number
  lng: number
  reason: string
  gains: Record<string, number>
}

export interface AiEvidence {
  clause: string
  source: string
}

export interface AiSuggestion {
  text: string
  priority: Priority
}

export interface AiReport {
  /** true = 用了模板降级 */
  degraded: boolean
  diagnosis: string
  evidence: AiEvidence[]
  suggestions: AiSuggestion[]
}

/** 🎯 核心产物：体检报告（CONTRACT.md §4） */
export interface AuditReport {
  meta: ReportMeta
  isochrones: Isochrones
  facilities: Facilities
  blindspots: Blindspots
  score: Score
  siting: Siting[]
  ai_report: AiReport
}

/* ---------- §5 后端 API 的其余响应 ---------- */

export interface GeocodeResult {
  lat: number
  lng: number
  address: string
}

export interface HealthResult {
  /** 当前激活社区的 id */
  seed_community?: string | null
  /** 当前激活社区的显示名 */
  seed_name?: string | null
  /** 当前激活社区的默认中心（BD09）；首屏默认点用它 */
  seed_center?: { lat: number; lng: number } | null
  status: string
  data_mode: string
  llm: boolean
}

export interface SeedCommunity extends LatLng {
  id?: string
  name: string
  address?: string
}

export interface ApiErrorBody {
  error: { code: string; message: string }
}

/** 带类目与颜色的 POI（地图渲染用，前端派生自 Poi） */
export interface PoiDatum extends Poi {
  category: CategoryKey
  categoryName: string
  color: string
}

/** 盲区清单条目的展示辅助（前端派生，非契约字段） */
export const BLINDSPOT_LABELS: Record<string, string> = CATEGORY_NAMES

export function categoryName(key: string): string {
  return CATEGORY_NAMES[key as CategoryKey] ?? key
}

/** 等级配色（前端自定义） */
export const GRADE_COLORS: Record<Grade, string> = {
  A: '#31d0a5',
  B: '#5aa9ff',
  C: '#ffb84d',
  D: '#ff5d73'
}
