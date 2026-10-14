"""体检流水线编排 —— `GET /api/audit` 的全部业务逻辑。

数据流（SOLUTION §3）：

    地址/点选 -> 中心点 BD09
      -> [等时圈] build_isochrones_multi：一次采样出 5/10/15 min 三档（省 2.3 倍配额）
      -> [POI] 2 组多关键字并集检索 -> 6 级清洗流水线 -> 圈内设施
      -> [覆盖统计] 数量/密度/最近步行耗时/覆盖率评分
      -> [可达性场] 解析耗时场 + 边界带真实算路校正
      -> [盲区] 连通域聚合为多边形 + MCLP 补点建议
      -> [评分] 8 维雷达 + 综合分
      -> [报告] AI 解读（**防御性调用**，S4 不存在时用最简模板兜底）

⚠️ AI 层必须防御性调用：`app.ai` 可能还没写完（S4 负责），
   `from app.ai import generate_report` 失败时本模块自己产出模板结果，**API 绝不能 500**。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import numpy as np

from ..baidu.errors import QuotaExhaustedError
from ..config import settings
from ..constants import (
    BLIND_SPOT_CATEGORIES,
    BLIND_SPOT_DISTANCE_M,
    CATEGORY_KEYS,
    DEFAULT_MINUTES,
    FACILITY_CATEGORIES,
)
from ..core.geo import LocalProjection
from ..core.isochrone import SampleConfig, extract_from_field, ray_roots, sample_field
from ..providers import (
    FallbackMatrixRouteProvider,
    get_center,
    get_place_provider,
    get_route_provider,
    meta_of,
    resolve_mode,
    set_center,
)
from ..providers.base import SeedMissingError
from ..schemas.report import AuditReport
from . import blindspot as blindspot_svc
from . import poi as poi_svc
from .accessibility import build_access_grid, correct_with_routes
from .coverage import compute_coverage
from .geometry import (
    empty_geojson,
    geom_area_m2,
    geom_centroid_latlng,
    geom_from_wkb_hex,
    geom_to_lnglat_rings,
    geom_to_wkb_hex,
    geometry_stats,
    largest_polygon,
    to_geojson,
)
from .score import compute_score

log = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

#: 每档一个 SampleConfig（mock/replay 可以多加密；live 要省配额）
CFG_FIELDS = dict(rays=36, dr=200.0)
#: 盲区可达性格网步长（米）
BLIND_CELL_M = 125.0
#: 盲区边界带真实算路预算（每类）
BLIND_ROUTE_BUDGET = 240
#: 覆盖统计的真实算路预算（总数）
COVERAGE_WALK_BUDGET = 60


def _fresh_client():
    """每次审计用**独立的**账本与缓存。

    为什么不复用 `get_client()` 单例：单例账本会把「今天已用多少配额」跨请求累积，
    demo / 测试里跑几次就把 100 次 POI 配额烧光，之后所有请求都会快速失败。
    真实生产部署应当复用（这样才有跨请求的配额保护），所以这里做成显式选择：
    只有 live 模式复用单例，其余模式用内存账本。
    """
    from ..baidu.cache import FileCache
    from ..baidu.client import BaiduClient
    from ..baidu.quota import QuotaLedger

    mode = resolve_mode()
    if mode == "live":
        from ..baidu.client import get_client

        return get_client()
    return BaiduClient(
        ledger=QuotaLedger(db_path=":memory:"),
        cache=FileCache(settings.cache_dir),
    )


def _sample_config(mode: str, densify: int = 3) -> SampleConfig:
    """live 模式必须省配额：routematrix 不可用时 directionlite 是 1 请求 1 条，
    加密点越多越贵，所以压制到 1 个/穿越区间；mock/replay 零配额，可以多加密。"""
    d = densify if mode in ("mock", "replay") else 1
    return SampleConfig(rays=CFG_FIELDS["rays"], dr=CFG_FIELDS["dr"], densify_per_crossing=d)


@dataclass
class AuditOptions:
    lat: float | None = None
    lng: float | None = None
    address: str | None = None
    minutes: tuple[int, ...] = DEFAULT_MINUTES
    city: str = "上海市"
    mode: str | None = None
    densify: int = 3
    force_mock: bool = False


@dataclass
class AuditOutcome:
    report: dict
    notes: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0


def _now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# AI 层：防御性调用
# --------------------------------------------------------------------------
def _template_ai_report(report: dict, reason: str) -> dict:
    """最简模板兜底。**任何情况下都必须返回契约 §4 的 ai_report 结构。**"""
    fac = (report.get("facilities") or {}).get("categories") or []
    bs = (report.get("blindspots") or {}).get("polygons") or []
    score = report.get("score") or {}
    gaps = [c for c in fac if (c.get("count") or 0) == 0]
    weak = sorted(fac, key=lambda c: c.get("coverage_score") or 0)[:3]
    parts = [
        f"本次体检综合得分 {score.get('total', 0)}（{score.get('grade', 'D')} 级）。",
    ]
    if weak:
        parts.append("覆盖最薄弱的类别依次为：" + "、".join(
            f"{c['name']}（{c['count']} 处，覆盖率 {c['coverage_score'] * 100:.0f}%）" for c in weak) + "。")
    if gaps:
        parts.append("完全缺失的类别：" + "、".join(c["name"] for c in gaps) + "。")
    if bs:
        total_km2 = sum((p.get("area_m2") or 0) for p in bs) / 1e6
        parts.append(f"识别到 {len(bs)} 处服务盲区，合计 {total_km2:.3f} km²，"
                     f"主要集中在步行 1 km 内无菜市场/药店/小学的区域。")
    else:
        parts.append("未识别到 1 km 内无菜市场/药店/小学的服务盲区。")
    suggestions = []
    for p in bs[:3]:
        names = "、".join(blindspot_svc._zh(k) for k in (p.get("missing") or []))
        suggestions.append({"text": f"建议在 ({p['sample_point']['lat']:.5f}, {p['sample_point']['lng']:.5f}) "
                                    f"一带补充{names or '民生设施'}，可覆盖约 "
                                    f"{(p.get('area_m2') or 0) / 1e4:.1f} hm² 盲区。",
                            "priority": "high" if len(p.get("missing") or []) >= 2 else "medium"})
    suggestions.append({"text": "对覆盖率低于 50% 的类别，结合《上海市15分钟社区生活圈规划导则》"
                                "的设施配置要求，优先补足菜市场与药店。", "priority": "medium"})
    return {
        "degraded": True,
        "diagnosis": "".join(parts),
        "evidence": [
            {"clause": "《城市居住区规划设计标准》GB 50180-2018 —— 居住区配套公共服务设施配置要求",
             "source": "GB 50180-2018"},
            {"clause": "《完整居住社区建设标准（试行）》—— 社区配套设施配置要求",
             "source": "住房和城乡建设部"},
            {"clause": "《上海市15分钟社区生活圈规划导则》—— 步行 15 分钟可达的基本服务要求",
             "source": "上海市规划和自然资源局"},
        ],
        "suggestions": suggestions[:4],
        "_fallback_reason": reason,
    }


def build_ai_report(report: dict) -> dict:
    """调用 S4 的 AI 层；不存在或抛错时退回模板。**永不抛异常。**

    `enable_llm=None` = 交给 AI 层按自己的 LLMConfig 判断（它自己读 .env 的 LLM_* 变量，
    本模块不碰、也不该碰这些配置）。
    """
    payload = {k: v for k, v in report.items() if not k.startswith("_")}
    try:
        from app.ai import generate_report  # type: ignore
    except Exception as e:  # ImportError 或其他导入期异常
        return _template_ai_report(report, f"app.ai 不可用（{type(e).__name__}: {e}）")
    try:
        result = generate_report(payload, enable_llm=None)
        if not isinstance(result, dict):
            return _template_ai_report(report, "app.ai 返回了非 dict")
        for key in ("degraded", "diagnosis", "evidence", "suggestions"):
            result.setdefault(key, _template_ai_report(report, "")[key])
        return result
    except Exception as e:  # noqa: BLE001 —— 契约要求永不 500
        log.warning("AI 层调用失败，已降级为模板：%s", e)
        return _template_ai_report(report, f"app.ai 调用异常（{type(e).__name__}: {e}）")


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def _snapshot_address() -> str:
    """快照里的社区地址，用作 meta.address 的兜底（用户只给了坐标时）。"""
    try:
        from ..providers import get_bundle

        b = get_bundle()
        return str(b.index.get("address") or b.index.get("name") or "")
    except Exception:
        return ""


def _resolve_center(opts: AuditOptions, mode: str) -> tuple[tuple[float, float], str, list[str]]:
    """返回 (中心 BD09, 地址, 备注)。"""
    notes: list[str] = []
    if opts.lat is not None and opts.lng is not None:
        center = (float(opts.lat), float(opts.lng))
        addr = opts.address or ""
        if not addr:
            snap = _snapshot_address()
            if snap:
                addr = snap
                notes.append(f"未提供地址，meta.address 采用快照社区地址：{snap}")
        return center, addr, notes

    if opts.address:
        from ..providers import get_geocode_provider

        gp = get_geocode_provider(mode)
        if gp is not None:
            try:
                hit = gp.geocode(opts.address)
                if hit:
                    return (float(hit[0]), float(hit[1])), opts.address, notes
            except SeedMissingError as e:
                notes.append(f"地址快照未命中（{e}），已回退到样例社区中心点")
            except QuotaExhaustedError as e:
                notes.append(f"地理编码配额不足，已回退到样例社区中心点：{e.message}")
            except Exception as e:  # noqa: BLE001
                notes.append(f"地理编码失败（{type(e).__name__}），已回退到样例社区中心点")
        # mock 模式：用确定性偏移把地址映射到样例社区内的一个点
        if mode == "mock":
            base = get_center()
            h = sum(ord(c) for c in opts.address)
            dx = ((h % 9) - 4) * 40.0
            dy = (((h // 9) % 9) - 4) * 40.0
            m_lat = 111132.0
            m_lng = m_lat * np.cos(np.radians(base[0]))
            notes.append("mock 模式：地址按确定性哈希映射到样例社区内的一个点（零网络）")
            return (base[0] + dy / m_lat, base[1] + dx / m_lng), opts.address, notes
        return get_center(), opts.address or "样例社区", notes

    return get_center(), "样例社区中心点（未指定地址，使用默认中心）", notes


@dataclass
class _IsoView:
    """等时圈的轻量视图（与核心层 IsochroneResult 字段对齐，但几何单独传）。"""

    polygon_xy: list
    area_m2: float
    n_samples: int
    n_requests: int
    routes_charged: int
    uncertainty_band_ratio: float | None = None
    warnings: list = field(default_factory=list)


def _isochrone_payload(res, geom, proj: LocalProjection, minutes: int) -> dict:
    """把等时圈几何 + 统计转成契约 §4 的 isochrones[key]。"""
    if geom is None or geom.is_empty:
        return {
            "target_seconds": float(minutes * 60),
            "area_m2": 0.0,
            "n_samples": int(res.n_samples),
            "routes_charged": int(res.routes_charged),
            "uncertainty_band_ratio": res.uncertainty_band_ratio,
            "geometry": empty_geojson(),
            "_degraded": True,
            "_warnings": list(res.warnings or []) + ["等时圈为空，几何为占位"],
        }
    rings = geom_to_lnglat_rings(geom, proj)
    return {
        "target_seconds": float(minutes * 60),
        "area_m2": round(float(largest_polygon(geom).area), 1),
        "n_samples": int(res.n_samples),
        "routes_charged": int(res.routes_charged),
        "uncertainty_band_ratio": (round(res.uncertainty_band_ratio, 4)
                                   if res.uncertainty_band_ratio is not None else None),
        "geometry": to_geojson(rings),
        "_warnings": list(res.warnings or []),
    }


def run_audit(opts: AuditOptions) -> AuditOutcome:
    t0 = time.perf_counter()
    mode = opts.mode or resolve_mode()
    notes: list[str] = []
    reasons: list[str] = []

    center, address, cnotes = _resolve_center(opts, mode)
    notes.extend(cnotes)
    if mode == "mock":
        set_center(center)
    proj = LocalProjection(*center)

    route_provider = get_route_provider(mode)
    place_provider = get_place_provider(mode)

    # ---- 等时圈：**一次采样出三档**（CONTRACT §6.1.1，省 2.3 倍配额）----
    # 用核心层暴露的中间层 sample_field / extract_from_field：
    # 一次射线采样 + 一次 IDW 重建，然后零成本抽出 300/600/900 s 三条等值线。
    targets = tuple(float(m * 60) for m in opts.minutes)
    cfg = _sample_config(mode, opts.densify)
    bundle = sample_field(route_provider, center, max(targets), cfg)
    iso_res: dict[float, object] = {}
    iso_geoms: dict[float, object] = {}
    for t in sorted(targets):
        try:
            geom = extract_from_field(bundle, t)
        except Exception as e:  # noqa: BLE001
            reasons.append(f"{t:.0f}s 等时圈提取失败（{type(e).__name__}: {e}）")
            geom = None
        iso_geoms[t] = geom
        warnings: list[str] = []
        if geom is None or geom.is_empty:
            warnings.append(f"{t:.0f}s 档位等值线提取为空（该档位内可能没有任何格点）")
        iso_res[t] = _IsoView(
            polygon_xy=[], area_m2=geom_area_m2(geom),
            n_samples=len(bundle.samples), n_requests=bundle.n_requests,
            routes_charged=bundle.routes_charged, warnings=warnings,
        )

    # 不确定带：用同一份采样的射线求根给出上界比值
    try:
        crossings, _ratio = ray_roots(bundle.samples, max(targets), cfg.rays)
        r_first = [iv[0][0] for iv in crossings.values() if iv]
        r_last = [iv[-1][1] for iv in crossings.values() if iv]
        if r_first and r_last and sum(r_first) > 0:
            band = (sum(r_last) / len(r_last)) ** 2 / (sum(r_first) / len(r_first)) ** 2 - 1.0
            for t in targets:
                if t in iso_res:
                    iso_res[t].uncertainty_band_ratio = round(float(band), 4)
    except Exception as e:  # 不确定带是加分项，失败不影响主流程
        notes.append(f"不确定带计算跳过（{type(e).__name__}: {e}）")

    isochrones: dict[str, dict] = {}
    iso_geoms_by_min: dict[str, object] = {}
    for m in opts.minutes:
        res = iso_res.get(float(m * 60))
        if res is None:
            continue
        payload = _isochrone_payload(res, iso_geoms.get(float(m * 60)), proj, m)
        iso_geoms_by_min[str(m)] = iso_geoms.get(float(m * 60))
        warns = payload.pop("_warnings", [])
        degraded = payload.pop("_degraded", False)
        if degraded:
            reasons.extend(warns or [f"{m} 分钟等时圈为空"])
        isochrones[str(m)] = payload

    main_key = str(max(opts.minutes))
    main_geom = iso_geoms_by_min.get(main_key)
    main_rings = geom_to_lnglat_rings(main_geom, proj) if main_geom is not None else []
    main_area = geom_area_m2(main_geom) if main_geom is not None else 0.0
    notes.append(f"等时圈几何部件：{geometry_stats(main_geom)}（取最大部件作为分析域）")

    # ---- POI：2 次请求覆盖 8 类 ----
    radius = 1500
    main_radius_km = 1.0
    if main_geom is not None:
        minx, miny, maxx, maxy = largest_polygon(main_geom).bounds
        # 外接圆半径 × 1.1（SOLUTION §4.3 第 2 步的硬性约束：绝不能把半径开大）
        main_radius_km = 0.5 * float(np.hypot(maxx - minx, maxy - miny)) / 1000.0
        radius = int(min(2000, max(800, 1.1 * main_radius_km * 1000)))
    raw_records, fetch_notes, place_metas = poi_svc.fetch_raw(place_provider, center, radius)
    notes.extend(fetch_notes)

    # 越界率自检（SOLUTION §4.3 第 6 步）：
    # 百度圆形检索在半径超出城市边界时会**退化为全市检索**，症状是返回大量远距离 POI。
    # 判据用「是否超出请求半径本身」（而不是等时圈），并把结果分为
    #   * 圈外但仍在检索半径内 -> 正常（正是本地精确裁剪要处理的部分）
    #   * 超出检索半径       -> 可疑，记入降级原因
    for meta in place_metas:
        beyond = 0
        outside_ring = 0
        for item in meta.results:
            loc = (item.get("location") or {})
            try:
                d = float(np.hypot((float(loc["lat"]) - center[0]) * 111132.0,
                                   (float(loc["lng"]) - center[1]) * 111132.0 * np.cos(np.radians(center[0]))))
            except Exception:
                continue
            if d > radius:
                beyond += 1
            if d > main_radius_km * 1000.0:
                outside_ring += 1
        meta.out_of_bounds_ratio = round(beyond / max(1, len(meta.results)), 4)
        notes.append(
            f"检索半径 {radius} m：{len(meta.results)} 条结果中 {outside_ring} 条在等时圈外"
            f"（本地精确裁剪剔除，零配额）、{beyond} 条超出检索半径"
        )
        if meta.out_of_bounds_ratio > 0.20:
            reasons.append(
                f"POI 检索有 {meta.out_of_bounds_ratio:.0%} 的结果超出请求半径，"
                f"该次检索可能已退化为大范围检索"
            )

    cleaning = poi_svc.build_cleaning_result(raw_records, main_rings if main_rings else None)

    # ---- 覆盖统计 ----
    coverage = compute_coverage(cleaning.records, main_area, center, route_provider,
                                walk_budget=COVERAGE_WALK_BUDGET)
    notes.extend(coverage.notes)

    # ---- 可达性场 + 盲区 ----
    by_cat = {k: np.array([[r.lng, r.lat] for r in cleaning.records if r.category == k], dtype=float)
              for k in CATEGORY_KEYS}
    for k in CATEGORY_KEYS:
        if by_cat[k].size == 0:
            by_cat[k] = np.zeros((0, 2), dtype=float)
    facilities_xy: dict[str, np.ndarray] = {}
    for k, lnglat in by_cat.items():
        if len(lnglat) == 0:
            facilities_xy[k] = np.zeros((0, 2))
        else:
            facilities_xy[k] = np.array([proj.to_xy(lat, lng) for lng, lat in lnglat], dtype=float)

    blind = blindspot_svc.BlindSpotResult()
    t_real: dict[str, np.ndarray] = {}
    blind_routes = 0
    blind_cells_corrected = 0
    analytic = get_analytic_model_safe()
    if main_geom is not None and not largest_polygon(main_geom).is_empty:
        grid = build_access_grid(analytic, center, main_geom, facilities_xy, BLIND_CELL_M)
        corr = correct_with_routes(
            grid, BLIND_SPOT_CATEGORIES, facilities_xy, route_provider, center,
            threshold_m=BLIND_SPOT_DISTANCE_M, max_routes=BLIND_ROUTE_BUDGET,
        )
        notes.extend(corr.notes)
        t_real = corr.t_real
        blind_routes = corr.routes_charged
        blind_cells_corrected = corr.cells_corrected
        blind = blindspot_svc.detect_blindspots(grid, t_real, BLIND_SPOT_DISTANCE_M, model=analytic)
        notes.extend(blind.notes)
        siting = blindspot_svc.suggest_siting(blind, proj)
    else:
        siting = []
        reasons.append("等时圈为空，已跳过盲区识别")

    blind_dicts = []
    for p in blind.polygons:
        clat, clng = geom_centroid_latlng(p.geom, proj)
        blind_dicts.append({
            "area_m2": round(p.area_m2, 1),
            "missing": list(p.missing),
            "lat": clat, "lng": clng,
            "wkb": geom_to_wkb_hex(p.geom),
        })

    # ---- 评分 ----
    score = compute_score(coverage.stats)

    # ---- 汇总 meta ----
    degradation: list[str] = list(reasons)
    for p in (route_provider, place_provider):
        m = meta_of(p)
        if m.get("degraded"):
            degradation.extend(m.get("reasons") or [])
        for r in (m.get("reasons") or []):
            if m.get("source") in ("replay", "hybrid") and r not in degradation:
                degradation.append(r)
    try:
        client = _fresh_client()
        degradation.extend(client.degrade_reasons)
        quota = client.quota_snapshot()
    except Exception:
        quota = {"routes_used": 0, "routes_limit": settings.api_daily_route_limit,
                 "poi_used": 0, "poi_limit": settings.api_daily_poi_limit}

    seen: set[str] = set()
    degrade_reasons = [r for r in degradation if r and not (r in seen or seen.add(r))]
    degraded = bool(degrade_reasons) or any(
        (iso.get("area_m2") or 0) <= 0 for iso in isochrones.values()
    )

    report: dict = {
        "meta": {
            "center": {"lat": round(center[0], 6), "lng": round(center[1], 6)},
            "address": address,
            "generated_at": _now_iso(),
            "data_mode": mode,
            "degraded": degraded,
            "degrade_reasons": degrade_reasons,
            "quota": {
                "routes_used": int(quota.get("routes_used", 0)),
                "routes_limit": int(quota.get("routes_limit", settings.api_daily_route_limit)),
                "poi_used": int(quota.get("poi_used", 0)),
                "poi_limit": int(quota.get("poi_limit", settings.api_daily_poi_limit)),
            },
        },
        "isochrones": isochrones,
        "facilities": {
            "categories": [{
                "key": s.key, "name": s.name, "query": s.query, "count": s.count,
                "density_per_km2": s.density_per_km2,
                "nearest_walk_seconds": s.nearest_walk_seconds,
                "coverage_score": s.coverage_score,
                "pois": s.pois,
            } for s in coverage.stats],
            "cleaning_funnel": cleaning.counters.as_funnel(),
        },
        "blindspots": {
            "total_area_m2": round(blind.total_area_m2, 1),
            "polygons": [{
                "area_m2": round(p["area_m2"], 1),
                "missing": list(p["missing"]),
                "sample_point": {"lat": round(p["lat"], 6), "lng": round(p["lng"], 6)},
                "geometry": to_geojson(geom_to_lnglat_rings(
                    geom_from_wkb_hex(p["wkb"]), proj)),
            } for p in blind_dicts],
        },
        "score": {
            "total": score.total,
            "grade": score.grade,
            "dimensions": [{
                "key": d.key, "name": d.name, "score": d.score,
                "weight": d.weight, "raw_count": d.raw_count,
            } for d in score.dimensions],
        },
        "siting": [{"lat": s["lat"], "lng": s["lng"], "reason": s["reason"], "gains": s["gains"]}
                   for s in siting],  # 严格按契约字段（area_m2/missing 只进 _debug）
        "_debug": {
            "cleaning_log": cleaning.log,
            "notes": notes,
            "siting_detail": siting,
            # 每类实际做了真实算路校正的格网数（不是域内总格网数）
            "blindspot_corrected_cells": {k: int(np.isfinite(v).sum()) for k, v in (t_real or {}).items()},
            "coverage_routes": coverage.routes_charged,
            "blind_routes": blind_routes,
            "blind_cells_corrected": blind_cells_corrected,
            "place_requests": [{"query": m.query, "n": len(m.results),
                                "out_of_bounds_ratio": m.out_of_bounds_ratio} for m in place_metas],
        },
    }
    report["ai_report"] = build_ai_report(report)
    if "_fallback_reason" in report["ai_report"]:
        report["_debug"]["ai_fallback_reason"] = report["ai_report"].pop("_fallback_reason")
    try:  # AI 层诊断（可选，零成本）
        from app.ai import get_last_diagnostics  # type: ignore

        report["_debug"]["ai_diagnostics"] = get_last_diagnostics()
    except Exception:
        pass

    elapsed = time.perf_counter() - t0
    report["_debug"]["elapsed_s"] = round(elapsed, 2)

    # 自检：Pydantic 严格校验（字段名/类型与 CONTRACT §4 对齐）
    try:
        AuditReport.model_validate({k: v for k, v in report.items() if not k.startswith("_")})
    except Exception as e:  # noqa: BLE001
        report["meta"]["degraded"] = True
        report["meta"]["degrade_reasons"].append(f"报告结构校验失败：{e}")
        log.error("AuditReport 校验失败：%s", e)

    return AuditOutcome(report=report, notes=notes, elapsed_s=elapsed)


def get_analytic_model_safe():
    from ..providers import get_analytic_model

    try:
        return get_analytic_model()
    except Exception:
        from ..providers.barriers import zhangjiang_model

        return zhangjiang_model()
