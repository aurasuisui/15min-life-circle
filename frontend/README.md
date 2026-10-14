# 前端 · 「15 分钟生活圈」智能体检与规划助手

> Vue 3 + Vite + TypeScript 单页应用。地图用**百度地图 JSAPI 4.0**，图表用 **ECharts**，状态管理 **Pinia**，样式全部自研（**不引任何 UI 组件库**）。
> 接口字段严格对齐 [`docs/CONTRACT.md`](../docs/CONTRACT.md) §4（体检报告 JSON）与 §5（后端 API）。

---

## 1. 快速开始

```bash
cd frontend
npm install
cp .env.example .env.local     # 填入浏览器端 AK（见 §3）
npm run dev                    # http://127.0.0.1:5173/
npm run build                  # vue-tsc 类型检查 + 生产构建 → dist/
npm run preview                # 预览生产构建 http://127.0.0.1:4173/
node scripts/smoke.mjs         # 无头 Chrome 冒烟自测（见 §8）
```

开发期 Vite 已把 `/api` 代理到 `http://localhost:8000`（见 `vite.config.ts`），后端未启动时页面会**自动回退到离线数据**（见 §5）。

---

## 2. 目录结构

```
frontend/
├─ public/mock/audit.sample.json    # §4 结构的离线样例报告（8 类设施 / 3 个等时圈 / 2 个盲区 / 完整 score）
├─ scripts/make-audit-mock.mjs      # 用确定性算法重新生成上面这份样例数据
├─ src/
│  ├─ api/api.ts                    # 请求封装（超时 / 错误归一 / 契约结构校验 / mock 回退入口）
│  ├─ charts/echarts.ts             # ECharts 按需注册（只打包用到的图表）
│  ├─ components/
│  │  ├─ TopBar.vue                 # 地址输入（/api/geocode）、快捷社区、数据来源徽标
│  │  ├─ SidePanel.vue              # 中心点信息 / 时间档位 / 图层开关 / 配额 / 图例
│  │  ├─ MapView.vue                # BMapGL 地图：等时圈 · POI · 盲区 · AreaMask · 选点交互
│  │  ├─ ReportPanel.vue            # 右侧报告栏
│  │  ├─ BlindspotList.vue          # 盲区清单（点击 flyTo）
│  │  ├─ AiReportCard.vue           # diagnosis / evidence / suggestions
│  │  └─ charts/*.vue               # 雷达图 / 柱状图（三视图）/ 清洗漏斗 / 评分仪表盘
│  ├─ composables/useEchart.ts      # ECharts 生命周期与 resize 封装
│  ├─ map/baidu.ts                  # JSAPI 4.0 加载器、几何转换、距离/像素换算
│  ├─ map/poi-layer.ts              # 单 canvas 自绘 POI 图层（替代 DOM Marker）
│  ├─ map/bus.ts                    # 报告面板 → 地图 的指令总线（flyTo / 高亮）
│  ├─ stores/audit.ts               # Pinia：报告、档位、图层、选点、降级状态
│  └─ types/report.ts               # §4 全部字段的 TS 类型（严格对齐契约）
└─ vite.config.ts                   # /api → localhost:8000 代理
```

---

## 3. 环境变量（AK 脱敏）

| 变量 | 说明 |
| --- | --- |
| `VITE_BAIDU_MAP_BROWSER_AK` | **浏览器端** AK，只能从 `import.meta.env` 读取，代码中**不硬编码** |

`.env.example` 只放占位符：

```
VITE_BAIDU_MAP_BROWSER_AK=your_browser_ak_here
```

> ⚠️ `VITE_` 前缀的变量会被**打包进前端产物**，因此服务端 AK / SK / LLM API Key **绝不能**写成 `VITE_` 变量。
> 本项目前端只持有浏览器端 AK；所有需要 SN 签名的服务端能力一律走后端 `/api/**`。

### AK 被拒时怎么排查

若地图区域出现「地图未能加载」，按控制台报错对号入座：

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 白屏 + 控制台 `APP不存在`/`AK有误` | 用了服务端 AK，或 AK 类型不对 | 换成控制台里「浏览器端」类型的 AK |
| 控制台提示权限/白名单相关错误 | **Referer 白名单**未包含当前地址 | 控制台 → 应用设置 → Referer 白名单，加入 `localhost`、`127.0.0.1`（生产再加正式域名） |
| 脚本请求超时 | 网络不可达 `api.map.baidu.com` | 检查代理/DNS |

> 本机开发若使用 `http://127.0.0.1:5173`，白名单里 **`localhost` 与 `127.0.0.1` 两项都要加**（二者 Referer 不同）。

---

## 4. 地图实现要点（选型理由）

1. **JSAPI 4.0，而不是 JSAPI GL。**
   官方已把 JSAPI GL 标注为「历史版本，不建议用于新项目」，新项目推荐 JSAPI 4.0；加载地址为
   `https://api.map.baidu.com/api?type=webgl&v=4.0&ak=***`。
   实测该版本**同时挂载 `window.BMapGL` 与 `window.BMap`**，因此代码统一使用 `BMapGL.*` 命名空间
   （`Map` / `Point` / `Polygon` / `Marker` / `AreaMask`），既跟随官方推荐版本，又保留 GL 的 API 语义。

2. **不引入 BMapGLLib（第三方 GL 工具库）。**
   它是社区维护的 GL 时代工具库，与 JSAPI 4.0 的兼容性没有官方保证，属于演示风险点。
   「等时圈热力图」改用**更稳妥且信息量等价**的方案：**5/10/15 三层嵌套多边形 + 色阶填充 + 图例**，
   内层自然叠加出更深的颜色，一眼即可看出可达范围的层次；盲区与圈外压暗同理用矢量覆盖物表达。

3. **POI 点位不用 DOM Marker。**
   全部 POI 由 `src/map/poi-layer.ts` 在一张**自绘 canvas** 上绘制（约 3 个绘制批次），
   无论 60 个还是 6000 个点都只占 1 个 DOM 节点；缩放/平移期间淡出自绘层，避免与 WebGL 底图错位。
   若运行环境缺少 `pointToPixel` / `pointToOverlayPixel` 投影 API，则自动降级为 `BMapGL.Circle`
   矢量圆（同样由 GL 渲染，不是 DOM）。当前模式在地图右上角实时显示。

4. **圈外压暗用 `BMapGL.AreaMask`，并带兜底。**
   优先 `new BMapGL.AreaMask(ring, {...})`；若该版本构造形态不同或不可用，则退化为
   「等时圈外接矩形之外铺 4 块深色矩形」。当前实现方式同样显示在地图右上角，便于现场排查。

5. **JSAPI 4.0 里没有 `AreaMask`，改用 `MapMask` + `map.addFocusMask()`。**
   实测 `typeof BMapGL.AreaMask === 'undefined'`（GL 时代的类在 4.0 已被移除），
   4.0 对应能力是 `new BMapGL.MapMask(ring, options)` 配合 `map.addFocusMask(mask)` / `removeFocusMask(mask)`。
   只有该路径不可用时才回退到「外接矩形之外铺 4 块深色矩形」。当前生效方式实时显示在地图右上角。
   压暗强度做了**像素级自测**：同一张截图里，15 分钟圈内平均亮度 **169.8**，圈外 **92.5**（越低越暗）。

6. **⚠️ 地图加载必须绕开官方入口脚本的 `document.write`。**
   官方入口 `/api?type=webgl&v=4.0&ak=***` 返回的只是**引导脚本**，它内部用
   `document.write('<script src="/getscript?...">')` 注入真正的 1.1 MB 地图库。
   而 `document.write` 只在**文档解析期间**有效——一旦像本项目这样在页面加载完成后用
   `createElement('script')` 异步注入，这段 write 会被浏览器**静默忽略**，
   结果是**不报错、也永远拿不到 `window.BMapGL`**，地图区域一直 loading（本项目实测踩到过）。
   因此 `src/map/baidu.ts` 直接加载真实库地址 `/getscript?v=4.0&ak=...`，并按引导脚本的做法
   预设 `BMAP_PROTOCOL` / `BMapGL_loadScriptTime`；若该地址失败，再回退到「临时劫持 document.write」加载入口脚本。

7. **三种选点方式**：顶栏地址输入（`/api/geocode` → `/api/audit`）、地图单击落点、拖拽中心标记；
   落点后浮出「在此处体检」确认条，避免误触即重算。

---

## 5. 离线可开发（mock 回退）

- `public/mock/audit.sample.json` 是一份**结构完全符合契约 §4** 的样例报告：
  8 类设施（共 62 个 POI）、5/10/15 三个等时圈、2 处盲区、8 维评分与完整 `ai_report`。
- 启动流程：`GET /api/health` 探活 → 在线则 `GET /api/audit`；**任何一步失败**（网络错误、超时、
  非 2xx、JSON 非法、或返回结构不符合契约）都会自动回退到该 mock，并在报告栏顶部与侧栏明确标注
  「离线 mock 数据 / 已降级」，**绝不静默**。
- 因此前端可以在后端完全未启动的情况下独立开发、演示与截图。
- 想重新生成样例数据：`node scripts/make-audit-mock.mjs`（确定性算法，输出可复现）。

---

## 6. 响应式断点

| 视口宽度 | 布局 |
| --- | --- |
| ≥ 1280px | **三栏**：左分析面板（320px） + 地图（自适应） + 右报告栏（418px） |
| 1024–1279px | **两栏**：地图 + 报告栏；左侧面板收成可切换的浮层抽屉（顶栏「分析选项」） |
| < 1024px | 地图在上、报告在下，纵向堆叠 |

---

## 7. 自测（`scripts/smoke.mjs`）

零额外依赖（只用 Node 内置 `fetch` / `WebSocket` + 本机 Chrome 的 CDP）：

```bash
node scripts/smoke.mjs                                  # 默认打 http://127.0.0.1:5173/
node scripts/smoke.mjs "http://127.0.0.1:4173/?debug=1" # 验证生产构建（?debug=1 才暴露地图实例）
```

覆盖 18 项断言：JSAPI 挂载、地图 canvas、POI 自绘层是否真的画出了像素、ECharts 画布数量、
**地图上是否真的有等时圈/盲区多边形覆盖物**（`map.getOverlays()` 计数）、**圈外压暗的圈内/圈外亮度对比**、
**点击盲区清单是否真的把地图飞过去并高亮**、
时间档位多选切换、以及**后端不可用时是否回退离线数据并给出明确提示**；
console 有 error 即判失败。产物：`.smoke/*.png` 截图 + JSON 报告。退出码 0 = 全绿。

---

## 8. 图表清单（右栏）

| 图表 | 说明 |
| --- | --- |
| 评分仪表盘 | 综合分 0~100 + A/B/C/D 等级色带 + 8 维条形列表 |
| 雷达图 | 8 维设施得分（满分 10），虚线为 7.5 达标基准 |
| 柱状图 | 三视图切换：**数量 / 密度 / 最近步行耗时**（步行视图带 15 分钟阈值线） |
| 清洗漏斗 | `cleaning_funnel`：检索原始 → 去重 → 类型纠错 → 圈内有效，并标注每级损耗 |
| 盲区清单 | 面积、占比、缺失类目、抽样点；点击地图 flyTo 并高亮 |
| AI 报告 | `diagnosis`（支持 `**加粗**`）、`evidence`（规范条款 + 出处）、`suggestions`（高/中/低优先级） |
