# 接口契约（FROZEN · 2026-10-14）

> **本文件是并行开发的唯一真相源。所有子任务必须严格遵守，不得自行更改字段名。**
> 如需变更，先在 `docs/CONTRACT-CHANGELOG.md` 记录并通知协调者。

---

## 0. 仓库与目录

```
15min-life-circle/
├─ backend/                      # S1 负责（app/ai/** 归 S4）
│  ├─ pyproject.toml
│  ├─ app/
│  │  ├─ core/                   # ✅ 已完成，勿动：geo.py monotonic.py field.py isochrone.py
│  │  ├─ baidu/                  # BaiduClient（SN 签名/令牌桶/配额账本/错误分类）
│  │  ├─ providers/              # live | replay | mock 三态
│  │  ├─ services/               # POI 清洗 / 覆盖统计 / 可达性 / 盲区 / 评分
│  │  ├─ ai/                     # S4 负责：DeepSeek 客户端 + RAG 报告 + 模板降级
│  │  ├─ schemas/                # Pydantic 模型（严格对齐本契约）
│  │  └─ api/                    # FastAPI 路由
│  ├─ tests/  scripts/
│  └─ bench/                     # ✅ 已有 benchmark_isochrone.py + result.txt
├─ frontend/                     # S2 负责（Vue3 + Vite + TS）
├─ docs/                         # S3 负责 docs/design/**
│  ├─ REQUIREMENTS.md  SOLUTION.md  CONTRACT.md
│  └─ design/01-api-strategy.md 02-isochrone.md 03-poi-cleaning.md 04-blindspot.md 05-quota-and-fallback.md
├─ scripts/                      # S3 负责 run.ps1 / run.sh
├─ Dockerfile  docker-compose.yml  LICENSE  README.md   # S3 负责
└─ data/seed/                    # S1 产出
```

**已完成且冻结（任何人不得修改）**：`backend/app/core/{geo,monotonic,field,isochrone}.py`、`backend/scripts/selftest.py`、`scripts/check_ak.py`、`backend/bench/**`。

---

## 1. 运行环境

- Python 3.13，虚拟环境在 `backend/.venv`（**已装好** contourpy / shapely / scipy / numpy / fastapi / uvicorn / pydantic / pydantic-settings / httpx / pytest / respx）
- 调用方式：`backend/.venv/Scripts/python.exe`（Windows）
- **不要重装依赖**，需要新依赖先说明
- **无 Docker**（S3 写 Dockerfile 但不本机验证）
- 所有中文文件用 UTF-8。**Windows 控制台是 GBK**，Python 打印中文可能乱码——调试时把结果写进文件再读

## 2. 环境变量（`.env` 已存在，已被 gitignore）

```
VITE_BAIDU_MAP_BROWSER_AK=<浏览器端AK>
BAIDU_MAP_SERVER_AK=<服务端AK>
BAIDU_MAP_SERVER_SK=<服务端SK>
NEXT_PUBLIC_...  (无)
LLM_ENABLED=true
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=<LLM_API_KEY>
LLM_MODEL=deepseek-flash
LLM_EFFORT=low
LLM_TIMEOUT_S=40
LLM_MAX_TOKENS=4000
DATA_MODE=hybrid        # live | replay | hybrid
```

> ⚠️ `.env` 里的 AK/SK/API Key **绝不能**出现在任何被提交的文件里（含前端 `VITE_` 变量、README、文档）。前端只能拿到浏览器端 AK。

---

## 2.5 🚦 闸门 G0 结论：前端地图用 **JSAPI 4.0**（2026-10-14 实测通过）

用浏览器端 AK `6H1wEw...` 在 `http://127.0.0.1` 下无头 Chrome 实测：

| 加载器 | `window.BMapGL` | `window.BMap` | 建图 | Polygon | Marker | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| `?type=webgl&v=1.0`（GL） | ✅ object | ❌ undefined | ✅ | ✅ | ✅ | 可用，但官方已标「历史版本」 |
| `?type=webgl&v=4.0`（4.0） | ✅ object | ✅ object | ✅ | ✅ | ✅ | **采用** |

**三条结论（前端必须遵守）**：
1. **加载器统一用**：`https://api.map.baidu.com/api?type=webgl&v=4.0&ak=<浏览器端AK>`
2. **API 命名空间用 `BMapGL.*`** —— 4.0 保留了该命名空间（向后兼容），所以 `BMapGL.Map / Point / Polygon / Marker / GeoJsonLayer` 写法在两个版本下都成立
3. **浏览器端 AK 的 Referer 白名单已允许 127.0.0.1 / localhost**，本地开发无需额外配置

> ⚠️ 关于「等时圈热力图」：**不要依赖 BMapGLLib**（第三方 GL 工具库，与 4.0 的兼容性未验证）。
> 稳妥方案 = **5/10/15 三层多边形 + 色阶填充**（已验证可行）；
> 想要连续热力则用自绘 canvas 叠加后端返回的耗时格网（有余力再做）。

---

## 3. 百度地图接口：**已实测的可用性**（务必遵守）

| 接口 | 路径 | 状态 | 备注 |
| --- | --- | --- | --- |
| 地理编码 | `/geocoding/v3/` | ✅ 可用 | 返回 BD09 |
| 坐标转换 | `/geoconv/v1/` | ✅ 可用 | |
| 地点检索 v3 | `/place/v3/around` | ✅ 可用 | 个人开发者可用 |
| 轻量路线规划 | `/directionlite/v1/walking` | ✅ 可用 | 1 请求 1 条路线 |
| **批量算路** | `/routematrix/v2/{walking,driving,riding}` | ❌ **返回 211** | 签名已排除，疑为服务权限；**必须做双 provider** |

### 3.1 SN 签名（已实现并验证，直接复用 `scripts/check_ak.py` 的 `make_sn`）

```python
def bd_encode(v): return urllib.parse.quote(str(v), safe="", encoding="utf-8")
def make_sn(path, params, sk):
    query = "&".join(f"{k}={bd_encode(v)}" for k, v in params.items())
    return hashlib.md5(bd_encode(f"{path}?{query}{sk}").encode()).hexdigest()
```
- 参数顺序：**必须是 URL 中出现的顺序**；`ak` 必须在 params 里；`sn` 最后计算、最后拼接
- 官方向量：`/geocoder/v2/?address=百度大厦&output=json&ak=yourak` + `sk=yoursk` → `7de5a22212ffaa9e326444c75a58f9a0`

### 3.2 硬性限制（实测/官方文档）

- 步行批量算路单次**最多 50 条路线**；**配额按最终路线数计费**，不是请求数
- 步行起终点距离 > 200 km 报参数错
- `place` 的 `page_size` 最大 **20**；`radius` 超城市边界会退化为全市检索 → **必须传 `radius_limit=true`**
- 多关键字并集：`query=药店$综合医院$诊所`，`$` 分隔，**最多 10 个**
- 个人开发者配额：地理编码 5000/日、**地点检索 100/日**、步行路线规划(轻量) 5000/日、坐标转换 5000/日、**QPS 均为 3**
- **✅ 已实测：多关键字并集按【请求】计费**（2 次请求 = 2% 配额）

### 3.3 错误码（按服务分别处理，语义不一致）

| status | 含义 | 处置 |
| --- | --- | --- |
| 0 | 成功 | — |
| 4 / 302 | 配额耗尽 | **快速失败，不重试**，切 replay |
| 401 | QPS 超限 | 退避重试 |
| 101 / 200 | AK 非法 | 报错终止 |
| 210 | IP 白名单失败 | 报错终止 |
| **211** | SN 校验失败 **或** 服务未开通 | 先查签名，再查权限 |
| 240 | 服务被禁用/AK 类型错 | 报错终止 |

---

## 4. 🎯 核心产物：体检报告 JSON（**最关键的契约**）

`GET /api/audit` 返回此结构。前端、AI 层、文档全部对齐它。

```jsonc
{
  "meta": {
    "center": { "lat": 31.2045, "lng": 121.5990 },   // BD09
    "address": "上海市浦东新区海科路1650号",
    "generated_at": "2026-10-14T15:00:00+08:00",
    "data_mode": "live",              // live | replay | mock
    "degraded": false,                // 任一环节降级则为 true
    "degrade_reasons": [],            // ["POI 配额不足，使用快照", ...]
    "quota": { "routes_used": 432, "routes_limit": 5000,
               "poi_used": 3, "poi_limit": 100 }
  },

  "isochrones": {                     // key 为分钟数字符串
    "5":  { "target_seconds": 300,  "area_m2": 812345.0,  "n_samples": 432,
            "routes_charged": 432,  "uncertainty_band_ratio": 0.18,
            "geometry": { "type": "Polygon", "coordinates": [[[121.60,31.20], ...]] } },
    "10": { "...": "同上" },
    "15": { "...": "同上" }
  },

  "facilities": {
    "categories": [
      {
        "key": "market",              // 机器名：market/pharmacy/primary_school/clinic/supermarket/elderly_care/bus_stop/park
        "name": "菜市场",
        "query": "农贸市场$菜市场$生鲜市场",   // 实际使用的检索词
        "count": 12,
        "density_per_km2": 14.8,
        "nearest_walk_seconds": 180,
        "coverage_score": 0.82,       // 0~1
        "pois": [ { "uid": "xxx", "name": "张江菜市场", "lat": 31.20, "lng": 121.60,
                    "address": "...", "tag": "市场", "walk_seconds": 180 } ]
      }
    ],
    "cleaning_funnel": { "raw": 320, "deduped": 268, "corrected": 254, "inside": 186 }
  },

  "blindspots": {
    "total_area_m2": 123456.0,
    "polygons": [
      { "area_m2": 45678.0,
        "missing": ["market", "pharmacy"],
        "sample_point": { "lat": 31.21, "lng": 121.61 },
        "geometry": { "type": "Polygon", "coordinates": [[[121.61,31.21], ...]] } }
    ]
  },

  "score": {
    "total": 76.5,                    // 0~100
    "grade": "B",                     // A/B/C/D
    "dimensions": [
      { "key": "medical", "name": "医疗", "score": 8.2, "weight": 0.15, "raw_count": 5 }
    ]
  },

  "siting": [                          // 补点建议，可为空数组
    { "lat": 31.21, "lng": 121.61, "reason": "该点 1km 内无菜市场", "gains": {"market": 1} }
  ],

  "ai_report": {
    "degraded": false,                 // true = 用了模板降级
    "diagnosis": "……",
    "evidence": [ { "clause": "《上海市15分钟社区生活圈规划导则》…", "source": "…" } ],
    "suggestions": [ { "text": "……", "priority": "high" } ]   // high | medium | low
  }
}
```

### 4.1 GeoJSON 约定
- **坐标系一律 BD09**；坐标顺序一律 `[lng, lat]`（GeoJSON 标准）
- 多边形**必须闭合**（首尾点相同）
- 所有几何用 EPSG 无关的裸坐标（前端直接用 BMapGL 渲染，BMapGL 接受 BD09 经纬度）

### 4.2 8 类设施与检索词（**冻结**）

| key | name | query（多关键字，`$` 分隔） | 分组 |
| --- | --- | --- | --- |
| `market` | 菜市场 | `农贸市场$菜市场$生鲜市场` | 购物 |
| `pharmacy` | 药店 | `药店$药房` | 医疗 |
| `primary_school` | 小学 | `小学` | 教育 |
| `clinic` | 社区医疗 | `社区卫生服务中心$诊所$综合医院` | 医疗 |
| `supermarket` | 超市便利店 | `超市$便利店` | 购物 |
| `elderly_care` | 养老设施 | `养老院$敬老院$日间照料中心` | 医疗 |
| `bus_stop` | 公交站 | `公交车站` | 交通 |
| `park` | 公园绿地 | `公园$广场` | 绿地 |

> 医疗组合并为 **1 次请求**（`药店$药房$社区卫生服务中心$诊所$综合医院$养老院$敬老院$日间照料中心`，8 个关键字 ≤ 10）；
> 购物 + 教育 + 交通 + 绿地合并为 **1 次请求**（`农贸市场$菜市场$生鲜市场$超市$便利店$小学$公交车站$公园$广场`，9 个关键字）。
> **总计 2 次请求覆盖 8 类**。若某类因 page_size 上限召回不足，允许追加 1 次补充请求（最多 4 次）。

---

## 5. 后端 API 契约

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | `{"status":"ok","data_mode":"...","llm":true/false}` |
| GET | `/api/geocode?address=xxx` | `{"lat":..,"lng":..,"address":".."}` |
| GET | `/api/audit?lat=..&lng=..` 或 `?address=..` | **完整体检报告 JSON（§4）** |
| GET | `/api/isochrone?lat=..&lng=..&minutes=15` | 单个等时圈对象 |
| GET | `/api/seed/communities` | 可用离线社区列表 |

- 全部返回 JSON；错误统一 `{"error": {"code": "...", "message": "..."}}`，HTTP 状态码语义正确
- 开发期 CORS 允许 `http://localhost:5173`

---

## 6. 模块接口（跨子任务调用点）

### 6.1 Provider（S1）
```python
# app/providers/base.py
class RouteProvider(Protocol):
    def walking_matrix(self, origin: tuple[float,float],
                       destinations: list[tuple[float,float]]) -> list[float | None]: ...
class PlaceProvider(Protocol):
    def search(self, query: str, center: tuple[float,float],
               radius: int = 2000, page_num: int = 0) -> dict: ...
```
```python
# app/providers/registry.py
def get_route_provider() -> RouteProvider: ...
def get_place_provider() -> PlaceProvider: ...
```
- `live`：走真实 API；`replay`/`hybrid` 未命中时读 `data/seed/`，再不行抛 `QuotaExhaustedError`
- **必须有 MockProvider**，使全部单元测试无需网络

### 6.1.1 ⭐ 多档位等时圈（**S1 必须用这个，不要分三次调用**）

`backend/app/core/isochrone.py` 新增（已通过测试）：

```python
def build_isochrones_multi(
    provider, center_latlng: tuple[float, float],
    targets_s: tuple[float, ...] = (300.0, 600.0, 900.0),   # ⚠️ 单位是【秒】
    cfg: SampleConfig | None = None,
    barriers_latlng=None,
) -> dict[float, IsochroneResult]: ...

# 另有可复用的中间层（需要自定义时用）：
def sample_field(provider, center_latlng, max_target_s=900.0, cfg=None, barriers_latlng=None) -> FieldBundle
def extract_from_field(bundle: FieldBundle, target_s: float)
```

**实测收益（backend/tests/test_isochrone_multi.py）**：
- 三档一起做 = **396 条路线**；分别跑 = **924 条** → **省 2.3 倍配额**
- directionlite 兜底模式下，采集耗时从 ~300 秒降到 ~130 秒
- 面积单调性验证通过：5min 0.258 → 10min 0.959 → 15min 2.360 km²

> ⚠️ **单位陷阱**：`build_isochrone(provider, center, target_s, ...)` 里 `target_s` 是**秒**不是分钟。
> 传 15.0 会被当成 15 秒（采样半径只有 26 米）。**一律用 `build_isochrones_multi` 并传 300/600/900。**

---

### 6.2 AI 层（S4）—— S1 只按此签名调用
```python
# app/ai/__init__.py
def generate_report(report: dict, *, enable_llm: bool | None = None) -> dict:
    """输入 §4 的完整报告 dict，返回 §4 的 ai_report 子对象。
    永不抛异常；任何失败都返回模板降级结果（degraded=True）。"""
```
> **S1 必须用 try/except 包裹此调用**：`app.ai` 不存在或抛错时，S1 自己产出一个最简模板结果，保证 API 不 500。

---

## 7. 编码约定

- Python：`from __future__ import annotations`；类型注解；docstring 中文；纯函数优先
- 不引入重型依赖（不要 geopandas）；新依赖必须先说明
- 测试放 `backend/tests/`，用 `pytest`；**同时保证 `backend/scripts/selftest.py` 风格的无依赖运行器可用**
- 前端：Vue 3 `<script setup>` + TS + Pinia + ECharts；不引重型 UI 库
- **提交前必须自测**：附上你运行命令与真实输出

---

## 8. Windows 环境注意事项

- 控制台 GBK，**中文输出可能乱码**；需要看中文时把结果写入文件再读
- PowerShell 里 `*>` 重定向产生 UTF-16 文件，读取时按 `utf16le` 解码
- Python 解释器：`backend/.venv/Scripts/python.exe`
- 网络出口在香港（代理），API 延迟偏高，超时设 ≥ 20s
