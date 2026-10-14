import type { Geometry, Isochrone, LatLng } from '@/types/report'

/**
 * 百度地图 JSAPI 加载器。
 *
 * 选型说明（闸门 G0 实测结论，2026-10-14）：
 *  - 官方已把 JSAPI **GL** 标注为「历史版本，不建议用于新项目」，新项目推荐 **JSAPI 4.0**；
 *    因此这里加载的是 4.0（`type=webgl&v=4.0`），已验证该版本同时挂载 `window.BMapGL`，
 *    故全项目继续沿用 `BMapGL.*` 命名空间（Map / Point / Polygon / Marker 均成立（AreaMask 在 4.0 已移除，改用 MapMask + addFocusMask））。
 *  - **不引入 BMapGLLib**（第三方 GL 工具库，与 4.0 的兼容性未经官方验证，属风险点）。
 *    「等时圈热力图」改用「5/10/15 三层多边形 + 色阶填充 + 图例」实现，视觉等价且零风险。
 *
 * AK 只从 `import.meta.env.VITE_BAIDU_MAP_BROWSER_AK` 读取，绝不硬编码。
 */
export const BROWSER_AK: string = (import.meta.env.VITE_BAIDU_MAP_BROWSER_AK ?? '').trim()

const LOAD_TIMEOUT_MS = 25000

let loading: Promise<void> | null = null

/**
 * ⚠️ 实测踩坑（务必保留此实现，不要改回「直接引官方入口脚本」）：
 * 官方入口 `/api?type=webgl&v=4.0&ak=...` 返回的只是一段 **引导脚本**，它内部用
 * `document.write('<script src="/getscript?...">')` 注入真正的 1.1 MB 地图库。
 * 而 `document.write` 只在**文档解析期间**有效：一旦我们用 `createElement('script')` 在
 * 页面加载完成后异步注入，这段 write 会被浏览器忽略（严重时还会清空文档），
 * 结果是**既不报错、也永远拿不到 window.BMapGL**（表现为地图区域一直 loading）。
 *
 * 因此这里直接加载真实库地址 `/getscript`（与引导脚本注入的 URL 完全一致），
 * 并按引导脚本的做法预设 `BMAP_PROTOCOL` / `BMapGL_loadScriptTime`；
 * 若该地址不可用，再回退到「劫持 document.write」的方式加载官方入口脚本。
 */
export function loadBaiduMap(): Promise<void> {
  if (loading) return loading
  loading = new Promise<void>((resolve, reject) => {
    if ((window as unknown as { BMapGL?: unknown }).BMapGL) return resolve()
    if (!BROWSER_AK) {
      return reject(new Error('未配置 VITE_BAIDU_MAP_BROWSER_AK：请在 frontend/.env.local 里填入浏览器端 AK（参考 .env.example）'))
    }

    const w = window as unknown as Record<string, unknown>
    w.BMAP_PROTOCOL = 'https'
    w.BMapGL_loadScriptTime = Date.now()

    const bundle =
      'https://api.map.baidu.com/getscript?v=4.0&ak=' +
      encodeURIComponent(BROWSER_AK) +
      '&services=&t=' +
      Date.now()

    let settled = false
    const started = Date.now()
    const timer = window.setInterval(() => {
      if ((window as unknown as { BMapGL?: unknown }).BMapGL) {
        window.clearInterval(timer)
        settled = true
        resolve()
      } else if (Date.now() - started > LOAD_TIMEOUT_MS) {
        window.clearInterval(timer)
        if (!settled) {
          settled = true
          reject(
            new Error(
              '百度地图 JSAPI 加载超时（>' +
                LOAD_TIMEOUT_MS / 1000 +
                's）：请检查网络是否可达 api.map.baidu.com，以及该浏览器端 AK 的 Referer 白名单是否包含当前地址'
            )
          )
        }
      }
    }, 120)

    const script = document.createElement('script')
    script.async = true
    script.src = bundle
    script.onerror = () => {
      // 直接取库失败 → 回退：劫持 document.write 后加载官方入口脚本
      try {
        loadViaBootstrap()
      } catch {
        if (!settled) {
          settled = true
          window.clearInterval(timer)
          reject(new Error('百度地图 JSAPI 脚本加载失败：请检查网络，或该 AK 的 Referer 白名单是否包含当前域名'))
        }
      }
    }
    document.head.appendChild(script)

    /** 回退路径：官方入口脚本内部用 document.write 注入真实库，这里把 write 重定向为 append */
    function loadViaBootstrap() {
      const doc = document as unknown as { write: (...args: string[]) => void }
      const origWrite = doc.write.bind(document)
      doc.write = (...args: string[]) => {
        const html = args.join('')
        const src = /<script[^>]+src="([^"]+)"/i.exec(html)
        if (src) {
          const s = document.createElement('script')
          s.async = true
          s.src = src[1]
          document.head.appendChild(s)
        }
        const href = /<link[^>]+href="([^"]+)"/i.exec(html)
        if (href) {
          const l = document.createElement('link')
          l.rel = 'stylesheet'
          l.href = href[1]
          document.head.appendChild(l)
        }
        if (!src && !href) origWrite(...args)
      }
      const boot = document.createElement('script')
      boot.async = true
      boot.src = 'https://api.map.baidu.com/api?type=webgl&v=4.0&ak=' + encodeURIComponent(BROWSER_AK)
      document.head.appendChild(boot)
    }
  })
  return loading
}

/** 取到 BMapGL 命名空间（带类型断言，避免在业务代码里到处 any） */
export function gl(): Record<string, any> {
  const g = (window as unknown as { BMapGL?: Record<string, any> }).BMapGL
  if (!g) throw new Error('BMapGL 尚未加载')
  return g
}

/** 一个 GeoJSON 几何 → BMapGL 可用的多边形环数组（兼容 Polygon / MultiPolygon） */
export function geometryToPaths(geometry: Geometry, PointCtor: any): any[][] {
  const rings: number[][][] =
    geometry.type === 'Polygon'
      ? geometry.coordinates
      : (geometry.coordinates as number[][][][]).flat()
  return rings
    .filter((ring) => Array.isArray(ring) && ring.length >= 3)
    .map((ring) => ring.map(([lng, lat]) => new PointCtor(lng, lat)))
}

/** 等时圈按档位从大到小排序（先画 15 分钟，保证 5 分钟图元在最上层） */
export function isochroneEntries(iso: Record<string, Isochrone>): { minutes: string; item: Isochrone }[] {
  return Object.entries(iso)
    .map(([minutes, item]) => ({ minutes, item }))
    .sort((a, b) => Number(b.minutes) - Number(a.minutes))
}

/** 局部平面近似：两点间米距离（1.5 km 尺度误差 < 0.1 m） */
export function metersBetween(a: LatLng, b: LatLng): number {
  const R = 6371008.8
  const rad = Math.PI / 180
  const x = (b.lng - a.lng) * rad * Math.cos(((a.lat + b.lat) / 2) * rad) * R
  const y = (b.lat - a.lat) * rad * R
  return Math.hypot(x, y)
}

/** 每个像素代表的米数（用于点选 POI 的命中半径） */
export function metersPerPixel(lat: number, zoom: number): number {
  return (156543.03392 * Math.cos((lat * Math.PI) / 180)) / Math.pow(2, zoom)
}

/** 中心点标记图标（内联 SVG，免去额外静态资源与 CDN 依赖） */
export const CENTER_MARKER_SVG =
  'data:image/svg+xml;charset=UTF-8,' +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="44" height="44" viewBox="0 0 44 44">' +
      '<circle cx="22" cy="22" r="16" fill="rgba(49,208,165,0.16)"/>' +
      '<circle cx="22" cy="22" r="10" fill="none" stroke="#31d0a5" stroke-width="2.5"/>' +
      '<circle cx="22" cy="22" r="4" fill="#31d0a5" stroke="#04231a" stroke-width="1.5"/>' +
      '<path d="M22 1v6M22 37v6M1 22h6M37 22h6" stroke="#31d0a5" stroke-width="2" stroke-linecap="round"/>' +
      '</svg>'
  )
