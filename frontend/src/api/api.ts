import type { AuditReport, GeocodeResult, HealthResult, Isochrone, SeedCommunity } from '@/types/report'

/** 网络出口在香港（代理），API 延迟偏高 → 超时不低于 20 s */
export const TIMEOUT_MS = 25000

export class ApiError extends Error {
  code: string
  status?: number
  constructor(message: string, code = 'UNKNOWN', status?: number) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }
}

type Params = Record<string, string | number | undefined | null>

function withQuery(path: string, params?: Params): string {
  if (!params) return path
  const usp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue
    usp.append(k, String(v))
  }
  const q = usp.toString()
  return q ? path + '?' + q : path
}

async function request<T>(path: string, params?: Params): Promise<T> {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS)
  const url = withQuery(path, params)
  try {
    const res = await fetch(url, { signal: ctrl.signal, headers: { Accept: 'application/json' } })
    const text = await res.text()
    let data: unknown = null
    if (text) {
      try {
        data = JSON.parse(text)
      } catch {
        throw new ApiError('响应不是合法 JSON（HTTP ' + res.status + '）', 'BAD_JSON', res.status)
      }
    }
    const body = data as { error?: { code?: string; message?: string } } | null
    if (!res.ok) {
      throw new ApiError(body?.error?.message ?? 'HTTP ' + res.status, body?.error?.code ?? 'HTTP_' + res.status, res.status)
    }
    if (body?.error) throw new ApiError(body.error.message ?? '后端返回错误', body.error.code ?? 'BACKEND_ERROR', res.status)
    return data as T
  } catch (e) {
    if (e instanceof ApiError) throw e
    const err = e as { name?: string; message?: string }
    if (err?.name === 'AbortError') throw new ApiError('请求超时（>' + TIMEOUT_MS / 1000 + 's）：' + url, 'TIMEOUT')
    throw new ApiError('网络请求失败：' + (err?.message ?? String(e)), 'NETWORK')
  } finally {
    clearTimeout(timer)
  }
}

/** 契约 §4 的最小结构校验：后端字段缺失时宁可回退 mock，也不要渲染出半张报告 */
export function isValidReport(r: unknown): r is AuditReport {
  const x = r as AuditReport | null
  if (!x || typeof x !== 'object') return false
  if (!x.meta || typeof x.meta !== 'object') return false
  if (!x.meta.center || typeof x.meta.center.lat !== 'number' || typeof x.meta.center.lng !== 'number') return false
  if (!x.isochrones || typeof x.isochrones !== 'object') return false
  if (!x.facilities || !Array.isArray(x.facilities.categories) || x.facilities.categories.length === 0) return false
  if (!x.facilities.cleaning_funnel || typeof x.facilities.cleaning_funnel.raw !== 'number') return false
  if (!x.blindspots || !Array.isArray(x.blindspots.polygons)) return false
  if (!x.score || typeof x.score.total !== 'number' || !Array.isArray(x.score.dimensions)) return false
  if (!x.ai_report || typeof x.ai_report.diagnosis !== 'string') return false
  return true
}

export const api = {
  /** GET /api/health */
  health: () => request<HealthResult>('/api/health'),

  /** GET /api/geocode?address=xxx */
  geocode: (address: string) => request<GeocodeResult>('/api/geocode', { address }),

  /** GET /api/audit?lat=..&lng=.. 或 ?address=.. —— 完整体检报告（契约 §4） */
  audit: (q: { lat?: number; lng?: number; address?: string }) => request<AuditReport>('/api/audit', q),

  /** GET /api/isochrone?lat=..&lng=..&minutes=15 */
  isochrone: (q: { lat: number; lng: number; minutes: number }) => request<Isochrone>('/api/isochrone', q),

  /** GET /api/seed/communities */
  seedCommunities: () => request<SeedCommunity[]>('/api/seed/communities'),

  /** 离线开发：读取 public/mock/audit.sample.json（结构同契约 §4） */
  mockReport: async (): Promise<AuditReport> => {
    const url = (import.meta.env.BASE_URL || '/') + 'mock/audit.sample.json'
    const res = await fetch(url, { cache: 'no-cache' })
    if (!res.ok) throw new ApiError('离线 mock 数据读取失败（HTTP ' + res.status + '）', 'MOCK_UNAVAILABLE', res.status)
    return (await res.json()) as AuditReport
  }
}
