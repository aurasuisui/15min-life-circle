/**
 * 生成前端离线 mock 数据：frontend/public/mock/audit.sample.json
 * 结构严格对齐 docs/CONTRACT.md §4。零依赖（node 内置即可）。
 * 用法：node scripts/make-audit-mock.mjs
 */
import { writeFileSync, mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const OUT = resolve(__dirname, '../public/mock/audit.sample.json')

const CENTER = { lat: 31.2045, lng: 121.5990 }
const ADDRESS = '上海市浦东新区海科路1650号'
const V = 1.25 // m/s 步行速度
const RAD = Math.PI / 180
const R_EARTH = 6371008.8

/* ---------- 确定性伪随机（保证每次生成结果一致） ---------- */
let seed = 20261014
const rnd = () => {
  seed = (seed * 1103515245 + 12345) % 2147483648
  return seed / 2147483648
}
const jitter = (amp) => (rnd() - 0.5) * 2 * amp
const round = (v, n = 6) => Number(v.toFixed(n))

/* 局部 ENU：米 -> BD09 经纬度 */
const toLngLat = (x, y) => [
  round(CENTER.lng + x / (111320 * Math.cos(CENTER.lat * RAD))),
  round(CENTER.lat + y / 110540)
]

/* 各档位的“真实可达边界”半径函数：随角度起伏，模拟路网密度差异 */
const BASE_R = { 5: 300, 10: 570, 15: 870 }
const R_FN = {
  5: (d) => 300 * (1 + 0.16 * Math.cos(3 * d + 0.7) + 0.07 * Math.cos(8 * d + 2.1) + 0.035 * Math.sin(15 * d + 0.4)),
  10: (d) => 570 * (1 + 0.14 * Math.cos(3 * d + 0.5) + 0.06 * Math.cos(9 * d + 1.7) + 0.03 * Math.sin(17 * d + 1.1)),
  15: (d) => 870 * (1 + 0.13 * Math.cos(2 * d + 0.35) + 0.05 * Math.cos(7 * d + 2.6) + 0.03 * Math.sin(13 * d + 0.9))
}
/* 河道 / 铁路造成的凹陷（张江段：东西向河道 + 东北向铁路） */
const NOTCH = (d) => {
  let k = 1
  const g = (c, w) => Math.exp(-Math.pow(((((d - c) % 360) + 540) % 360 - 180) / w, 2))
  k -= 0.20 * g(70, 9)
  k -= 0.13 * g(215, 12)
  k -= 0.09 * g(310, 16)
  k += 0.07 * g(155, 14)
  k += 0.05 * g(20, 11)
  return Math.max(k, 0.45)
}

const R = (minutes, d) => R_FN[minutes](d) * NOTCH(d)
const inside = (m, x, y) => {
  const d = (Math.atan2(y, x) / RAD + 360) % 360
  return Math.hypot(x, y) <= R(m, d)
}

/* POI 采样半径（米）：中心密、边缘疏；按 5/10/15 分钟三档分层 */
const pickRadius = () => {
  const u = rnd()
  if (u < 0.42) return Math.sqrt(rnd()) * 310
  if (u < 0.75) return Math.sqrt(rnd()) * 580
  return Math.sqrt(rnd()) * 880
}
/** 15 分钟圈面积（2.5 km 半径栅格蒙特卡洛，确定性） */
const DISC_AREA = (() => {
  let hit = 0
  const N = 40000
  for (let i = 0; i < N; i++) {
    const a = rnd() * 2 * Math.PI
    const r = Math.sqrt(rnd()) * 1200
    if (inside(15, Math.cos(a) * r, Math.sin(a) * r)) hit++
  }
  return (hit / N) * Math.PI * 1200 * 1200
})()

/* ---------- 8 类设施（key / name / query 冻结自 CONTRACT.md §4.2） ---------- */
const CATS = [
  { key: 'market', name: '菜市场', query: '农贸市场$菜市场$生鲜市场', n: 3, minR: 320, base: ['张江', '香楠', '丹桂路', '碧波路', '晨晖路', '科苑路', '高斯路', '祖冲之路', '川北', '金科路'], kind: ['菜市场', '农贸市场', '生鲜市场'] },
  { key: 'pharmacy', name: '药店', query: '药店$药房', n: 9, base: ['华氏', '国大', '益丰', '老百姓', '雷允上', '海王星辰', '第一医药', '好药师大'], kind: ['大药房', '药店', '药房'] },
  { key: 'primary_school', name: '小学', query: '小学', n: 2, minR: 260, base: ['张江高科', '浦东新区第二中心', '孙桥', '香楠', '万科翡翠', '金科路', '丹桂路', '碧波'], kind: ['小学', '实验学校小学部', '外国语小学'] },
  { key: 'clinic', name: '社区医疗', query: '社区卫生服务中心$诊所$综合医院', n: 4, base: ['张江社区卫生服务', '孙桥社区卫生服务', '浦东新区张江', '科苑路', '丹桂路', '香楠路', '碧波路'], kind: ['中心', '诊所', '门诊部', '综合医院'] },
  { key: 'supermarket', name: '超市便利店', query: '超市$便利店', n: 16, base: ['罗森', '全家', '好德', '联华', '永辉', '盒马', '良友', '快客', '便利蜂', '农工商'], kind: ['便利店', '超市', '生活超市', '生鲜超市'] },
  { key: 'elderly_care', name: '养老设施', query: '养老院$敬老院$日间照料中心', n: 2, minR: 380, base: ['张江', '浦东', '香楠', '金桂', '孙桥', '科苑'], kind: ['养老院', '敬老院', '日间照料中心', '长者照护之家'] },
  { key: 'bus_stop', name: '公交站', query: '公交车站', n: 21, base: ['海科路', '科苑路', '祖冲之路', '丹桂路', '金科路', '碧波路', '晨晖路', '高斯路', '张江地铁站', '川杨河'], kind: ['公交站', '公交枢纽站', '公交首末站'] },
  { key: 'park', name: '公园绿地', query: '公园$广场', n: 5, base: ['张江主题', '川杨河滨', '科苑', '香楠', '丹桂', '祖冲之'], kind: ['公园', '绿地', '广场', '口袋公园'] }
]
const road = ['海科路', '科苑路', '祖冲之路', '丹桂路', '金科路', '碧波路', '晨晖路', '高斯路', '香楠路', '川北公路']
const community = ['香楠小区', '丹桂苑', '碧波苑', '金桂苑', '张江新村', '科苑新村', '晨晖苑', '祖冲之路小区', '高科苑', '川杨河苑']

const pois = []
const perCat = {}
let uidN = 1000
for (const c of CATS) {
  const cnt = c.n
  perCat[c.key] = cnt
  const list = []
  let guard = 0
  while (list.length < cnt && guard++ < 20000) {
    const a = rnd() * Math.PI * 2
    const rr = pickRadius()
    const x = Math.cos(a) * rr, y = Math.sin(a) * rr
    if (rr < (c.minR ?? 80)) continue // 避免出现"步行 0 秒"的假数据；稀缺类目只出现在外圈
    if (!inside(15, x, y)) continue // 全部落在 15 分钟圈内
    const [lng, lat] = toLngLat(x, y)
    const walk = Math.round((rr * (1.12 + rnd() * 0.34)) / V)
    if (walk > 900) continue // 超过 900 s 即落在 15 分钟圈外
    const nm = c.base[Math.floor(rnd() * c.base.length)] + c.kind[Math.floor(rnd() * c.kind.length)]
    list.push({
      uid: 'mock' + uidN++,
      name: nm,
      lat, lng,
      address: '上海市浦东新区' + road[Math.floor(rnd() * road.length)] + (100 + Math.floor(rnd() * 1900)) + '号' + community[Math.floor(rnd() * community.length)] + '附近',
      tag: c.kind[Math.floor(rnd() * c.kind.length)],
      walk_seconds: walk
    })
  }
  list.sort((p, q) => p.walk_seconds - q.walk_seconds)
  pois.push(...list.map((p) => ({ ...p, __k: c.key })))
  c.__pois = list
}
const rawTotal = Math.round(pois.length * 3.6) // 模拟检索原始量（2 组多关键字请求 × page_size=20 × 多页 + 跨类重复与脏数据）

/* ---------- 等时圈几何 ---------- */
const ring = (m, n = 72) => {
  const pts = []
  for (let i = 0; i < n; i++) {
    const d = (i / n) * 360
    const r = R(m, d) * (1 + jitter(0.012))
    pts.push(toLngLat(Math.cos(d * RAD) * r, Math.sin(d * RAD) * r))
  }
  pts.push(pts[0])
  return pts
}
const areaOf = (pts) => {
  const p = pts.map(([lng, lat]) => [lng * 111320 * Math.cos(CENTER.lat * RAD), lat * 110540])
  let s = 0
  for (let i = 0; i < p.length - 1; i++) s += p[i][0] * p[i + 1][1] - p[i + 1][0] * p[i][1]
  return Math.abs(s / 2)
}
const MINUTES = [5, 10, 15]
const isochrones = {}
const rings = {}
for (const m of MINUTES) {
  const pts = ring(m)
  rings[m] = pts
  const nSamples = m === 5 ? 168 : m === 10 ? 300 : 432
  isochrones[String(m)] = {
    target_seconds: m * 60,
    area_m2: Number(areaOf(pts).toFixed(1)),
    n_samples: nSamples,
    routes_charged: nSamples,
    uncertainty_band_ratio: Number((0.12 + m * 0.004).toFixed(3)),
    geometry: { type: 'Polygon', coordinates: [pts] }
  }
}

/* ---------- 盲区（灰区）：等时圈内、但缺菜市场/药店/小学的成片区域 ---------- */
const blindCfg = [
  { c: 63, rad: 935, da: 42, missing: ['market', 'pharmacy', 'primary_school'], offs: [-260, 120, 60] },
  { c: 211, rad: 880, da: 38, missing: ['market', 'primary_school'], offs: [-140, 90, 320] }
]
const polygons = []
for (const cfg of blindCfg) {
  const pts = []
  const n = 44
  for (let i = 0; i < n; i++) {
    const d = cfg.c - cfg.da / 2 + (i / n) * cfg.da
    // 沿半径向内挖：环带宽度决定盲区面积
    const band = 215 + 105 * Math.sin((i / n) * Math.PI * 3 + cfg.c) + jitter(38)
    const r = cfg.rad - band
    pts.push(toLngLat(Math.cos(d * RAD) * r, Math.sin(d * RAD) * r))
  }
  for (let i = n; i >= 0; i--) {
    const d = cfg.c - cfg.da / 2 + (i / n) * cfg.da
    const r = cfg.rad * (1 + jitter(0.01))
    pts.push(toLngLat(Math.cos(d * RAD) * r, Math.sin(d * RAD) * r))
  }
  pts.push(pts[0])
  const a = areaOf(pts)
  const sa = (cfg.c + 4) * RAD, sr = cfg.rad - 280
  polygons.push({
    area_m2: Number(a.toFixed(1)),
    missing: cfg.missing,
    sample_point: (() => { const [lng, lat] = toLngLat(Math.cos(sa) * sr + cfg.offs[0], Math.sin(sa) * sr + cfg.offs[1]); return { lat, lng } })(),
    geometry: { type: 'Polygon', coordinates: [pts] }
  })
}

/* ---------- 评分：8 维 ---------- */
/* 每类设施的合理配置基准（15 分钟生活圈尺度，参考 GB 50180-2018 / 上海社区生活圈导则） */
const NEED = { market: 6, pharmacy: 8, primary_school: 3, clinic: 3, supermarket: 12, elderly_care: 4, bus_stop: 15, park: 5 }
const scoreOf = (c) => {
  const obs = c.__pois.length
  const q = Math.min(1, obs / NEED[c.key]) // 数量满足度
  const dPen = c.__pois[0].walk_seconds <= 600 ? 1 : c.__pois[0].walk_seconds <= 900 ? 0.85 : 0.65 // 最近一处可达性
  return Math.max(0.08, Math.min(0.97, 0.7 * q + 0.3 * dPen))
}
const byKey = Object.fromEntries(CATS.map((c) => [c.key, c]))
const DIMS = [
  { key: 'medical', name: '医疗', weight: 0.15, cats: ['clinic', 'pharmacy'] },
  { key: 'education', name: '教育', weight: 0.12, cats: ['primary_school'] },
  { key: 'elderly', name: '养老', weight: 0.1, cats: ['elderly_care'] },
  { key: 'commerce', name: '商业', weight: 0.2, cats: ['market', 'supermarket'] },
  { key: 'transit', name: '交通', weight: 0.13, cats: ['bus_stop'] },
  { key: 'green', name: '绿地', weight: 0.1, cats: ['park'] },
  { key: 'walkability', name: '步行友好', weight: 0.1, cats: [] },
  { key: 'diversity', name: '均衡性', weight: 0.1, cats: [] }
]
const catScore = Object.fromEntries(CATS.map((c) => [c.key, scoreOf(c)]))
const covered = CATS.filter((c) => c.__pois[0].walk_seconds <= 900).length
const dimensions = DIMS.map((d) => {
  if (d.key === 'walkability') {
    const sh5 = CATS.filter((c) => c.__pois[0].walk_seconds <= 300).length / CATS.length
    const sh10 = CATS.filter((c) => c.__pois[0].walk_seconds <= 600).length / CATS.length
    return { key: d.key, name: d.name, score: Number((10 * (0.65 * sh5 + 0.35 * sh10)).toFixed(1)), weight: d.weight, raw_count: covered }
  }
  if (d.key === 'diversity') {
    const vals = Object.values(catScore)
    const mean = vals.reduce((a, b) => a + b, 0) / vals.length
    const sd = Math.sqrt(vals.reduce((s, v) => s + (v - mean) ** 2, 0) / vals.length)
    return { key: d.key, name: d.name, score: Number(Math.max(2, Math.min(10, 10 - sd * 16)).toFixed(1)), weight: d.weight, raw_count: covered }
  }
  const sc = d.cats.reduce((s, k) => s + catScore[k], 0) / d.cats.length
  return { key: d.key, name: d.name, score: Number((sc * 10).toFixed(1)), weight: d.weight, raw_count: d.cats.reduce((s, k) => s + byKey[k].__pois.length, 0) }
})
// 维度分 0~10，权重和为 1 → 综合分 0~100
const total = Number((dimensions.reduce((s, d) => s + d.score * d.weight, 0) * 10).toFixed(1))
/* 等级阈值：A≥90 优秀 / B≥75 良好 / C≥60 达标待改进 / D<60 明显不足 */
const grade = total >= 90 ? 'A' : total >= 75 ? 'B' : total >= 60 ? 'C' : 'D'

/* ---------- 补点建议（落在盲区多边形内） ---------- */
const inPoly = (pt, coord) => {
  let hit = false
  for (let i = 0, j = coord.length - 1; i < coord.length; j = i++) {
    const [xi, yi] = coord[i], [xj, yj] = coord[j]
    if ((yi > pt.lat) !== (yj > pt.lat) && pt.lng < ((xj - xi) * (pt.lat - yi)) / (yj - yi) + xi) hit = !hit
  }
  return hit
}
const siting = []
const REASON = {
  market: '该点 1km 步行范围内无菜市场，建议结合社区商业网点补建生鲜超市或社区菜店',
  pharmacy: '该点 1km 步行范围内无药店，建议引入连锁药房或设置社区卫生服务站代配药窗口',
  primary_school: '该点 1km 步行范围内无小学，建议结合片区学位缺口统筹布局'
}
for (const p of polygons) {
  const coord = p.geometry.coordinates[0]
  for (const miss of p.missing.slice(0, 2)) {
    let cand = p.sample_point
    if (!inPoly(cand, coord)) {
      // 兜底：取多边形质心方向上的内部点
      const cx = coord.reduce((s, c) => s + c[0], 0) / coord.length
      const cy = coord.reduce((s, c) => s + c[1], 0) / coord.length
      cand = { lat: Number(cy.toFixed(6)), lng: Number(cx.toFixed(6)) }
    }
    siting.push({ lat: cand.lat, lng: cand.lng, reason: REASON[miss] || '该点设施覆盖不足，建议补点', gains: { [miss]: 1 } })
    break
  }
}

/* ---------- 组装报告 ---------- */
const insideTotal = CATS.reduce((s, c) => s + c.__pois.length, 0)
const report = {
  meta: {
    center: CENTER,
    address: ADDRESS,
    generated_at: '2026-10-14T15:00:00+08:00',
    data_mode: 'mock',
    degraded: true,
    degrade_reasons: ['后端 /api/audit 不可用，已回退前端离线 mock 数据（frontend/public/mock/audit.sample.json）'],
    quota: { routes_used: 900, routes_limit: 5000, poi_used: 2, poi_limit: 100 }
  },
  isochrones,
  facilities: {
    categories: CATS.map((c) => ({
      key: c.key,
      name: c.name,
      query: c.query,
      count: c.__pois.length,
      density_per_km2: Number((c.__pois.length / (isochrones['15'].area_m2 / 1e6)).toFixed(2)),
      nearest_walk_seconds: c.__pois[0].walk_seconds,
      coverage_score: Number(catScore[c.key].toFixed(3)),
      pois: c.__pois.map(({ __k, ...rest }) => rest)
    })),
    cleaning_funnel: {
      raw: rawTotal,
      deduped: Math.round(rawTotal * 0.84),
      corrected: Math.round(rawTotal * 0.79),
      inside: insideTotal
    }
  },
  blindspots: {
    total_area_m2: Number(polygons.reduce((s, p) => s + p.area_m2, 0).toFixed(1)),
    polygons
  },
  score: { total, grade, dimensions },
  siting,
  ai_report: {
    degraded: false,
    diagnosis: `该生活圈 15 分钟步行等时圈面积约 ${(isochrones['15'].area_m2 / 1e6).toFixed(2)} km²，共识别 8 类民生设施 ${insideTotal} 处，综合评分 ${total} 分（${grade} 级）。超市便利店、公交站与公园绿地覆盖良好，最近步行耗时均在 5 分钟以内；短板集中在**菜市场与养老设施**——两者数量偏少且最近一处步行耗时接近 10 分钟，其中 2 片区域（合计约 ${(polygons.reduce((s, p) => s + p.area_m2, 0) / 1e6).toFixed(2)} km²）在 1 公里步行范围内同时缺失菜市场与药店，属于典型服务盲区。等时圈呈明显非圆形态，东部受河道与铁路切割，可达边界较理论半径收缩约 13%~20%，说明单纯按直线距离评估会高估实际可达性。`,
    evidence: [
      { clause: '社区生活圈应配置菜市场（生鲜超市）、药店、社区卫生服务站等基本服务要素，步行 15 分钟可达覆盖率不低于 90%。', source: '《上海市 15 分钟社区生活圈规划导则》（沪府办发〔2021〕）' },
      { clause: '按照 5—10—15 分钟三个层级配置社区服务设施，其中菜市场服务半径宜为 500—800 m。', source: '《城市居住区规划设计标准》GB 50180-2018 附表 A' },
      { clause: '步行网络应连续、无障碍，过街设施与河道桥梁对实际步行可达性影响显著，评估须基于真实路网而非直线距离。', source: '本项目等时圈实测数据（等时圈面积/理论圆面积 = 0.79）' }
    ],
    suggestions: [
      { text: `在 ${blindCfg[0].c}° 方向盲区（约 ${(polygons[0].area_m2 / 1e6).toFixed(2)} km²、缺失菜市场/药店/小学）结合既有社区商业用房补建 1 处 200—400 m² 的生鲜超市或社区菜店，预计可消除该片区内约 60% 的设施空白面积。`, priority: 'high' },
      { text: `在 ${blindCfg[1].c}° 方向盲区（约 ${(polygons[1].area_m2 / 1e6).toFixed(2)} km²）引入连锁药房或依托社区卫生服务站设置代配药窗口，同时评估增设 1 所小学的学位供给。`, priority: 'high' },
      { text: '养老设施仅 ' + byKey.elderly_care.__pois.length + ' 处、最近步行 ' + Math.round(byKey.elderly_care.__pois[0].walk_seconds / 60) + ' 分钟，建议结合存量公建配套增设日间照料中心或长者照护之家，优先选址于老年人口密度较高的香楠小区一带。', priority: 'medium' },
      { text: '东部受河道与铁路分隔，跨河步行通道不足导致等时圈收缩明显；建议在丹桂路—科苑路之间增设跨河步行桥或优化既有桥梁的无障碍坡道，可提升该方向约 8% 的可达面积。', priority: 'medium' },
      { text: '公交站在 5 分钟圈内覆盖充分（' + byKey.bus_stop.__pois.length + ' 处），建议后续接入实时到站数据，将"可达"进一步细化到"可等候"。', priority: 'low' }
    ]
  }
}

mkdirSync(dirname(OUT), { recursive: true })
writeFileSync(OUT, JSON.stringify(report, null, 2) + '\n', 'utf8')
console.log('written -> ' + OUT)
console.log('categories=' + report.facilities.categories.length + ' isochrones=' + Object.keys(report.isochrones).length +
  ' blindspots=' + report.blindspots.polygons.length + ' pois=' + insideTotal +
  ' score=' + total + report.score.grade + ' funnel=' + JSON.stringify(report.facilities.cleaning_funnel))
console.log('area_km2=' + (report.isochrones['15'].area_m2 / 1e6).toFixed(3) + ' blindspot_km2=' + (report.blindspots.total_area_m2 / 1e6).toFixed(3))
console.log('counts=' + JSON.stringify(Object.fromEntries(report.facilities.categories.map(c => [c.key, c.count]))))
console.log('nearest=' + JSON.stringify(Object.fromEntries(report.facilities.categories.map(c => [c.key, c.nearest_walk_seconds]))))
console.log('dims=' + JSON.stringify(report.score.dimensions.map(d => d.name + ':' + d.score)))
console.log('weights_sum=' + report.score.dimensions.reduce((s, d) => s + d.weight, 0).toFixed(2))
console.log('siting=' + report.siting.length)
