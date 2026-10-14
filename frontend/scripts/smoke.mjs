/**
 * 前端冒烟自测（无头 Chrome + CDP，零额外依赖）
 *
 * 用法：node scripts/smoke.mjs [url] [outDir]
 *   默认 url = http://127.0.0.1:5173/
 *
 * 校验项：页面无 console error / 报告面板渲染 / 百度地图 canvas 挂载 / POI 自绘层生效 /
 *         等时圈与盲区覆盖物 / ECharts 画布数量；并输出整页截图供人工复核。
 * 退出码：0 = 全部通过，1 = 有失败项。
 */
import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const TARGET_URL = process.argv[2] ?? 'http://127.0.0.1:5173/'
const OUT = resolve(dirname(fileURLToPath(import.meta.url)), '..', process.argv[3] ?? '.smoke')
const CHROME =
  process.env.CHROME_PATH ?? 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const PORT = Number(process.env.CDP_PORT ?? 9333)
const WAIT_MS = Number(process.env.SMOKE_WAIT_MS ?? 9000)

mkdirSync(OUT, { recursive: true })

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function getJson(path, method = 'GET') {
  const res = await fetch('http://127.0.0.1:' + PORT + path, { method })
  return res.json()
}

const chrome = spawn(
  CHROME,
  [
    '--headless=new',
    '--disable-gpu',
    '--no-first-run',
    '--no-default-browser-check',
    '--hide-scrollbars',
    '--remote-debugging-port=' + PORT,
    '--user-data-dir=' + resolve(OUT, 'profile'),
    '--window-size=1680,1000',
    'about:blank'
  ],
  { stdio: 'ignore' }
)

let ws
let id = 0
const pending = new Map()
const consoleErrors = []
const consoleAll = []
const failedRequests = []
const baiduResponses = []

function send(method, params = {}) {
  return new Promise((res, rej) => {
    const msgId = ++id
    pending.set(msgId, { res, rej })
    ws.send(JSON.stringify({ id: msgId, method, params }))
  })
}

function cleanup(code) {
  try { ws?.close() } catch {}
  // Windows 下 Chrome 会派生多个子进程，必须整棵树结束，否则残留进程会一直占着 profile 目录
  try {
    if (process.platform === 'win32' && chrome.pid) {
      spawn('taskkill', ['/F', '/T', '/PID', String(chrome.pid)], { stdio: 'ignore' })
    } else {
      chrome.kill()
    }
  } catch {}
  setTimeout(() => process.exit(code), 500)
}

try {
  // 等 CDP 端口就绪
  let version = null
  for (let i = 0; i < 60; i++) {
    try {
      version = await getJson('/json/version')
      break
    } catch {
      await sleep(250)
    }
  }
  if (!version) throw new Error('无法连接无头 Chrome 的 CDP 端口 ' + PORT)

  const target = await getJson('/json/new?' + encodeURIComponent(TARGET_URL), 'PUT')
  ws = new WebSocket(target.webSocketDebuggerUrl)
  await new Promise((res, rej) => {
    ws.onopen = res
    ws.onerror = () => rej(new Error('CDP WebSocket 连接失败'))
  })

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data)
    if (msg.id && pending.has(msg.id)) {
      const { res, rej } = pending.get(msg.id)
      pending.delete(msg.id)
      msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result)
      return
    }
    if (msg.method === 'Runtime.consoleAPICalled') {
      const text = (msg.params.args ?? []).map((a) => a.value ?? a.description ?? a.type).join(' ')
      consoleAll.push({ level: msg.params.type, text })
      if (msg.params.type === 'error') consoleErrors.push(text)
    } else if (msg.method === 'Runtime.exceptionThrown') {
      const d = msg.params.exceptionDetails
      consoleErrors.push('EXCEPTION: ' + (d.exception?.description ?? d.text))
    } else if (msg.method === 'Network.loadingFailed') {
      failedRequests.push({ url: msg.params?.requestId, error: msg.params?.errorText })
    } else if (msg.method === 'Network.responseReceived') {
      const u = msg.params.response.url
      if (u.includes('api.map.baidu.com')) {
        baiduResponses.push({ url: u.slice(0, 120), status: msg.params.response.status })
      }
    }
  }

  await send('Page.enable')
  await send('Runtime.enable')
  await send('Network.enable')

  await sleep(WAIT_MS) // 等 JSAPI 加载 + 报告渲染

  const probe = await send('Runtime.evaluate', {
    expression: `JSON.stringify({
      title: document.title,
      hasBMapGL: typeof window.BMapGL !== 'undefined',
      mapCanvas: document.querySelectorAll('#bmap-container canvas').length,
      poiCanvas: !!document.querySelector('canvas.poi-canvas'),
      poiCanvasPainted: (() => { const c = document.querySelector('canvas.poi-canvas'); if (!c) return false; const g = c.getContext('2d'); const d = g.getImageData(0,0,c.width,c.height).data; let n=0; for (let i=3;i<d.length;i+=4000) if (d[i]>0) n++; return n>0 })(),
      mapError: document.querySelector('.map-fallback h4')?.innerText ?? null,
      overlays: (() => {
        const m = window.__bmap
        if (!m || typeof m.getOverlays !== 'function') return null
        const list = m.getOverlays()
        const names = list.map((o) => (o && o._className) || o?.constructor?.name || 'unknown')
        return { total: list.length, distinct: [...new Set(names)] }
      })(),
      mapAddress: document.querySelector('.map-addr')?.innerText?.trim() ?? null,
      layerModes: [...document.querySelectorAll('.map-tools .map-panel')].map(e=>e.innerText.trim()).join(' | '),
      score: document.querySelector('.score-total')?.innerText?.trim() ?? null,
      grade: document.querySelector('.score-meta .badge')?.innerText?.trim() ?? null,
      dimRows: document.querySelectorAll('.dim-row').length,
      bsItems: document.querySelectorAll('.bs-item').length,
      chartCanvas: document.querySelectorAll('.chart canvas').length,
      legendRows: document.querySelectorAll('.map-legend .legend-row').length,
      suggestions: document.querySelectorAll('.sug').length,
      addressChip: document.querySelector('.map-sub')?.innerText?.trim() ?? null,
      reportPanel: !!document.querySelector('.col-report'),
      sidePanel: !!document.querySelector('.col-side'),
      bodyWidth: document.body.clientWidth,
      workspaceCols: getComputedStyle(document.querySelector('.workspace')).gridTemplateColumns
    })`,
    returnByValue: true
  })
  const diag = JSON.parse(probe.result.value)

  const shot = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(resolve(OUT, 'map-1600.png'), Buffer.from(shot.data, 'base64'))

  // 圈外压暗的像素级校验：把截图回灌进页面，比较「15 分钟圈内」与「圈外」的平均亮度
  const maskExpr = `(async () => {
    const map = window.__bmap
    if (!map) return JSON.stringify({ error: 'no __bmap (需要 dev 模式)' })
    const G = window.BMapGL
    const c = map.getCenter()
    const rect = map.getContainer().getBoundingClientRect()
    const px = (dLng, dLat) => {
      const p = map.pointToPixel(new G.Point(c.lng + dLng, c.lat + dLat))
      return { x: Math.round(rect.left + p.x), y: Math.round(rect.top + p.y) }
    }
    const img = new Image()
    await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = 'data:image/png;base64,' + window.__shot })
    const cv = document.createElement('canvas')
    cv.width = img.width; cv.height = img.height
    const g = cv.getContext('2d')
    g.drawImage(img, 0, 0)
    const lum = (pt) => { const d = g.getImageData(pt.x, pt.y, 1, 1).data; return 0.2126*d[0] + 0.7152*d[1] + 0.0722*d[2] }
    // 圈内：中心附近 4 点；圈外：约 0.03° 外（≈2.9 km，仍在地图可视区内）
    const inside = [{x:c.lng,y:c.lat}, {x:c.lng+0.001,y:c.lat}, {x:c.lng,y:c.lat+0.001}, {x:c.lng-0.001,y:c.lat-0.001}].map(o=>lum(px(o.x-c.lng, o.y-c.lat)))
    const outside = [px(0.021,0.021), px(0.021,-0.021), px(-0.021,0.021), px(-0.021,-0.021)].map(lum)
    const avg = (a) => a.reduce((s,v)=>s+v,0)/a.length
    return JSON.stringify({ insideAvg: +avg(inside).toFixed(1), outsideAvg: +avg(outside).toFixed(1), darker: avg(outside) < avg(inside) * 0.8, mapRect: {left:Math.round(rect.left), top:Math.round(rect.top), w:Math.round(rect.width), h:Math.round(rect.height)} })
  })()`
  await send('Runtime.evaluate', { expression: 'window.__shot = "' + shot.data + '"' })
  const maskRes = await send('Runtime.evaluate', { expression: maskExpr, returnByValue: true, awaitPromise: true })
  const mask = JSON.parse(maskRes.result.value)

  /* ---------- 交互测试 ---------- */
  const before = await send('Runtime.evaluate', { expression: 'JSON.stringify({ c: window.__bmap ? window.__bmap.getCenter() : null })', returnByValue: true })

  // ① 点击盲区清单 → 地图应飞向该盲区且条目高亮
  await send('Runtime.evaluate', { expression: `document.querySelectorAll('.bs-item')[0]?.click()` })
  await sleep(2600)
  const afterBs = await send('Runtime.evaluate', {
    expression: `JSON.stringify({ c: window.__bmap ? window.__bmap.getCenter() : null, on: !!document.querySelector('.bs-item.on') })`,
    returnByValue: true
  })
  const bsRes = JSON.parse(afterBs.result.value)
  const c0 = JSON.parse(before.result.value).c
  const movedByBlindspot = c0 && bsRes.c ? Math.abs(bsRes.c.lat - c0.lat) + Math.abs(bsRes.c.lng - c0.lng) : 0
  const shotBs = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(resolve(OUT, 'blindspot-focus.png'), Buffer.from(shotBs.data, 'base64'))

  // ② 时间档位切换：取消 5 分钟档，chip 状态应同步
  await send('Runtime.evaluate', { expression: `[...document.querySelectorAll('.map-title .chip')].find(b=>b.innerText.includes('5 分钟'))?.click()` })
  await sleep(1200)
  const bandRes = await send('Runtime.evaluate', {
    expression: `JSON.stringify({ off: [...document.querySelectorAll('.map-title .chip')].filter(b=>!b.classList.contains('on')).map(b=>b.innerText.trim()), legendOnMap: document.querySelectorAll('.map-legend .legend-row').length })`,
    returnByValue: true
  })
  const bands = JSON.parse(bandRes.result.value)
  // 复原
  await send('Runtime.evaluate', { expression: `[...document.querySelectorAll('.map-title .chip')].find(b=>b.innerText.includes('5 分钟'))?.click()` })
  await sleep(800)

  // ③ 地址输入 → /api/geocode 失败（后端未启动）→ 应回退离线 mock 并明确提示
  await send('Runtime.evaluate', {
    expression: `(() => {
      const input = document.querySelector('.searchbox input')
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
      setter.call(input, '上海市浦东新区张江镇香楠路')
      input.dispatchEvent(new Event('input', { bubbles: true }))
      document.querySelector('.searchbox button').click()
    })()`
  })
  await sleep(6000)
  const fallbackRes = await send('Runtime.evaluate', {
    expression: `JSON.stringify({ notice: document.querySelector('.col-report .notice')?.innerText?.slice(0, 160) ?? null, score: document.querySelector('.score-total')?.innerText?.trim() ?? null, mapStillAlive: document.querySelectorAll('#bmap-container canvas').length > 0 })`,
    returnByValue: true
  })
  const fb = JSON.parse(fallbackRes.result.value)

  // 窄屏（两栏）截图
  await send('Emulation.setDeviceMetricsOverride', { width: 1180, height: 900, deviceScaleFactor: 1, mobile: false })
  await sleep(2200)
  const shot2 = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(resolve(OUT, 'map-1180.png'), Buffer.from(shot2.data, 'base64'))
  const probe2 = await send('Runtime.evaluate', {
    expression: `JSON.stringify({ width: document.body.clientWidth, cols: getComputedStyle(document.querySelector('.workspace')).gridTemplateColumns, sideHidden: getComputedStyle(document.querySelector('.col-side')).display })`,
    returnByValue: true
  })
  const diag2 = JSON.parse(probe2.result.value)

  const checks = [
    ['页面标题正确', diag.title.includes('15 分钟生活圈')],
    ['window.BMapGL 已挂载（JSAPI 4.0）', diag.hasBMapGL === true],
    ['地图未报错', diag.mapError === null],
    ['地图 canvas 已渲染', diag.mapCanvas > 0],
    ['POI 自绘层已绘制', diag.poiCanvasPainted === true],
    ['等时圈图例 5 项', diag.legendRows >= 5],
    ['地图覆盖物含等时圈/盲区多边形', diag.overlays ? diag.overlays.total >= 5 : true],
    ['右栏报告面板存在', diag.reportPanel === true],
    ['评分仪表盘已渲染', !!diag.score],
    ['8 维得分列表', diag.dimRows === 8],
    ['盲区清单条目', diag.bsItems >= 2],
    ['ECharts 画布 ≥ 4 个', diag.chartCanvas >= 4],
    ['AI 建议条目', diag.suggestions >= 2],
    ['圈外压暗生效（圈外亮度显著低于圈内）', mask.darker === true],
    ['点击盲区清单可定位地图', movedByBlindspot > 0.002 && bsRes.on === true],
    ['时间档位可多选切换', bands.off.length === 1],
    ['后端不可用时回退离线数据且明确提示', !!fb.notice && !!fb.score && fb.mapStillAlive === true],
    ['无 console error', consoleErrors.length === 0]
  ]
  const failed = checks.filter(([, ok]) => !ok)

  const result = {
    url: TARGET_URL,
    chrome: version.Browser,
    diagnostics: diag,
    maskContrast: mask,
    interactions: { blindspotFlyToDelta: +movedByBlindspot.toFixed(5), blindspotHighlighted: bsRes.on, bandsOff: bands.off, fallbackNotice: fb.notice },
    narrowViewport: diag2,
    baiduRequests: baiduResponses,
    consoleErrors,
    failedRequestsSample: failedRequests.slice(0, 5),
    checks: Object.fromEntries(checks),
    screenshots: [resolve(OUT, 'map-1600.png'), resolve(OUT, 'map-1180.png')],
    verdict: failed.length === 0 ? 'PASS' : 'FAIL'
  }
  console.log(JSON.stringify(result, null, 2))
  cleanup(failed.length === 0 ? 0 : 1)
} catch (e) {
  console.log(JSON.stringify({ verdict: 'ERROR', message: e.message, consoleErrors }, null, 2))
  cleanup(1)
}
