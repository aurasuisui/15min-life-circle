"""等时圈核心算法（SOLUTION.md §4.2 的实现）。

管线：
  射线采样布局 → 批量测时 → PAVA 单调化 → 射线求根（定位加密点 / 不确定带）
  → 局部加密 → IDW 重建耗时场（带屏障遮蔽）→ marching squares 提等值线
  → shapely 修复为多边形

关键设计（均有合成场景实测依据，见 bench/benchmark_isochrone.py）：
  * 多边形【不】从射线交点直接连 —— 内接星形会切掉凹角，系统性低估面积 5%~11%。
  * 阈值【外侧】必须有粗采样点 —— 只采内侧会让 IDW 无约束外推，IoU 崩到 0.62~0.79。
  * 一次采样同时服务 5/10/15 分钟三个阈值，成本不随档位数增加。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .field import build_grid, idw_interpolate, make_barrier_mask
from .geo import LocalProjection
from .monotonic import monotonicity_stats

MAX_ROUTES_PER_REQUEST = 50


@dataclass
class SampleConfig:
    rays: int = 36          # 射线数（10° 间隔）
    dr: float = 200.0       # 环距（米）
    walk_speed: float = 1.25
    max_factor: float = 1.4  # r_max = max_factor * v * T
    densify_per_crossing: int = 1
    grid_res: float = 25.0
    idw_k: int = 8
    idw_power: float = 2.0

    def r_max(self, target_s: float) -> float:
        return self.max_factor * self.walk_speed * target_s


@dataclass
class RaySample:
    ray_idx: int
    bearing_deg: float
    r_m: float
    x: float
    y: float
    t_s: float | None = None


@dataclass
class IsochroneResult:
    polygon_xy: list[tuple[float, float]]
    area_m2: float
    n_samples: int
    n_requests: int
    routes_charged: int
    monotonicity_modified_ratio: float
    uncertainty_band_ratio: float | None
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Step 1 — 射线采样布局
# --------------------------------------------------------------------------
def ray_layout(cfg: SampleConfig, target_s: float) -> list[RaySample]:
    r_max = cfg.r_max(target_s)
    n_rings = max(1, math.ceil(r_max / cfg.dr))
    out: list[RaySample] = []
    for i in range(cfg.rays):
        bearing = 360.0 * i / cfg.rays
        b = math.radians(bearing)
        for j in range(1, n_rings + 1):
            r = j * cfg.dr
            out.append(RaySample(i, bearing, r, r * math.sin(b), r * math.cos(b)))
    return out


# --------------------------------------------------------------------------
# Step 2 — 批量测时（按 50 条/请求分块）
# --------------------------------------------------------------------------
def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def measure(provider, origin_latlng, samples: list[RaySample], proj: LocalProjection) -> int:
    """调用 provider 填充 t_s。返回消耗的路线配额数。"""
    charged = 0
    for chunk in _chunks(samples, MAX_ROUTES_PER_REQUEST):
        dests = [proj.to_latlng(s.x, s.y) for s in chunk]
        durations = provider.walking_matrix(origin_latlng, dests)
        for s, d in zip(chunk, durations):
            # 0 或 None 一律视为无效，绝不当作「耗时 0」
            s.t_s = float(d) if d not in (None, 0, 0.0) else None
        charged += len(chunk)
    return charged


# --------------------------------------------------------------------------
# Step 4 — 射线求根（PAVA 单调化后找全部穿越区间）
# --------------------------------------------------------------------------
def ray_roots(
    samples: list[RaySample], target_s: float, rays: int
) -> tuple[dict[int, list[tuple[float, float]]], float]:
    """返回 {ray_idx: [(r_enter, r_exit), ...]} 与单调性修正比例。

    r_enter = 确定可达边界（保守下界）；r_exit = 可能可达边界（乐观上界）。
    可能出现多个区间 —— 正是「河对岸只有一座桥」这类场景。
    """
    by_ray: dict[int, list[RaySample]] = {}
    for s in samples:
        if s.t_s is not None:
            by_ray.setdefault(s.ray_idx, []).append(s)

    crossings: dict[int, list[tuple[float, float]]] = {}
    ratios: list[float] = []
    for i in range(rays):
        grp = sorted(by_ray.get(i, []), key=lambda s: s.r_m)
        if len(grp) < 2:
            continue
        stats = monotonicity_stats([g.t_s for g in grp])  # type: ignore[list-item]
        ratios.append(stats["modified_ratio"])
        ts = stats["fitted"]
        rs = [g.r_m for g in grp]

        intervals: list[tuple[float, float]] = []
        inside = ts[0] <= target_s
        start = 0.0
        for j in range(len(rs) - 1):
            t0, t1 = ts[j], ts[j + 1]
            if t0 <= target_s < t1 and inside:
                frac = (target_s - t0) / (t1 - t0) if t1 > t0 else 0.0
                intervals.append((start, rs[j] + frac * (rs[j + 1] - rs[j])))
                inside = False
            elif t1 <= target_s and not inside:
                inside = True
                start = rs[j]
        if inside:
            intervals.append((start, rs[-1]))
        if intervals:
            crossings[i] = intervals

    ratio = sum(ratios) / len(ratios) if ratios else 0.0
    return crossings, ratio


# --------------------------------------------------------------------------
# Step 3 — 阈值附近局部加密
# --------------------------------------------------------------------------
def densify(
    samples: list[RaySample], crossings: dict[int, list[tuple[float, float]]], cfg: SampleConfig, target_s: float
) -> list[RaySample]:
    """在每个穿越区间内插值加点，改善等值线在阈值附近的精度。"""
    extra: list[RaySample] = []
    for ray_idx, intervals in crossings.items():
        bearing = 360.0 * ray_idx / cfg.rays
        b = math.radians(bearing)
        for lo, hi in intervals:
            if hi <= lo:
                continue
            for k in range(1, cfg.densify_per_crossing + 1):
                r = lo + (hi - lo) * k / (cfg.densify_per_crossing + 1)
                extra.append(RaySample(ray_idx, bearing, r, r * math.sin(b), r * math.cos(b)))
    return extra


# --------------------------------------------------------------------------
# Step 5/6 — IDW 耗时场 → marching squares → shapely 多边形
# --------------------------------------------------------------------------
def contour_to_polygon(
    xs: np.ndarray,
    ys: np.ndarray,
    z: np.ndarray,
    mask: np.ndarray | None,
    target_s: float,
):
    """用 contourpy 的 filled() 提取覆盖区，再用 shapely 修复。

    filled() 天然闭合且自带洞层级，不需要自己配对环。
    contourpy 已知坑：等值线值恰等于某格点 z 值时会产生重复点 -> shapely 判 invalid，
    所以必须 make_valid。
    """
    import contourpy
    from shapely import make_valid
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    # contourpy 1.4 通过 z 的 masked array 表达不可通行区，没有 mask= 关键字参数
    z_in = np.ma.masked_array(z, mask=mask) if mask is not None else z
    cg = contourpy.contour_generator(
        x=xs, y=ys, z=z_in, fill_type="ChunkCombinedOffsetOffset"
    )
    filled = cg.filled(0.0, target_s)
    # contourpy 在「阈值内没有任何格点」时返回 [None]/[None]/[None]，
    # 只判 filled[0] is None 是不够的（列表本身非空、元素为 None），
    # 不挡住会在后面 len(offs2) 处抛 TypeError，导致 API 500。
    if not filled or len(filled) < 3:
        return None
    pts_all = filled[0][0] if filled[0] else None
    offs = filled[1][0] if len(filled) > 1 and filled[1] else None
    offs2 = filled[2][0] if len(filled) > 2 and filled[2] else None
    if pts_all is None or offs is None or offs2 is None or len(offs2) < 2:
        return None

    rings = []
    for i in range(len(offs2) - 1):
        seg = pts_all[offs[offs2[i]] : offs[offs2[i + 1]]]
        if len(seg) >= 3:
            rings.append(Polygon(seg))
    if not rings:
        return None

    valid = [make_valid(r) for r in rings]
    geom = unary_union(valid)
    if geom.is_empty:
        return None
    try:
        geom = geom.simplify(0.7 * (xs[1] - xs[0]), preserve_topology=True)
    except Exception:
        pass
    return geom


def build_isochrone(
    provider,
    center_latlng: tuple[float, float],
    target_s: float = 900.0,
    cfg: SampleConfig | None = None,
    barriers_latlng: list[list[tuple[float, float]]] | None = None,
) -> IsochroneResult:
    """完整等时圈流程。"""
    cfg = cfg or SampleConfig()
    proj = LocalProjection(*center_latlng)
    warnings: list[str] = []

    samples = ray_layout(cfg, target_s)
    charged = measure(provider, center_latlng, samples, proj)
    n_requests = math.ceil(len(samples) / MAX_ROUTES_PER_REQUEST)

    crossings, mono_ratio = ray_roots(samples, target_s, cfg.rays)
    if not crossings:
        warnings.append("没有任何射线穿越目标阈值：目标时长可能超出采样范围")

    extra = densify(samples, crossings, cfg, target_s)
    if extra:
        charged += measure(provider, center_latlng, extra, proj)
        n_requests += math.ceil(len(extra) / MAX_ROUTES_PER_REQUEST)
    all_samples = samples + extra

    valid = [s for s in all_samples if s.t_s is not None]
    if len(valid) < 6:
        raise ValueError(f"有效采样点不足（{len(valid)} 个），无法重建耗时场")

    r_max = cfg.r_max(target_s)
    xs, ys = build_grid(-r_max * 1.2, r_max * 1.2, -r_max * 1.2, r_max * 1.2, cfg.grid_res)

    barriers_xy = []
    for ring in barriers_latlng or []:
        barriers_xy.append(np.array([proj.to_xy(la, ln) for la, ln in ring]))
    mask = make_barrier_mask(xs, ys, barriers_xy) if barriers_xy else None

    z = idw_interpolate(
        np.array([[s.x, s.y] for s in valid]),
        np.array([s.t_s for s in valid], dtype=float),
        xs, ys,
        k=cfg.idw_k, power=cfg.idw_power,
        center_fill=(0.0, 0.0),
        center_radius=cfg.dr * 0.6,
        center_speed=cfg.walk_speed,
    )

    geom = contour_to_polygon(xs, ys, z, mask, target_s)
    if geom is None:
        warnings.append("等值线提取为空，请检查采样与阈值")
        return IsochroneResult([], 0.0, len(all_samples), n_requests, charged, mono_ratio, None, warnings)

    # 不确定带：确定可达 vs 可能可达的面积差
    band = None
    if crossings:
        r_first = [iv[0][0] for iv in crossings.values() if iv]
        r_last = [iv[-1][1] for iv in crossings.values() if iv]
        if r_first and r_last:
            a1 = math.pi * (sum(r_first) / len(r_first)) ** 2
            a2 = math.pi * (sum(r_last) / len(r_last)) ** 2
            if a1 > 0:
                band = (a2 - a1) / a1

    coords = list(geom.geoms[0].exterior.coords) if geom.geom_type == "MultiPolygon" and geom.geoms else (
        list(geom.exterior.coords) if geom.geom_type == "Polygon" else []
    )
    poly_xy = [(float(x), float(y)) for x, y in coords]

    return IsochroneResult(
        polygon_xy=poly_xy,
        area_m2=float(geom.area),
        n_samples=len(all_samples),
        n_requests=n_requests,
        routes_charged=charged,
        monotonicity_modified_ratio=mono_ratio,
        uncertainty_band_ratio=band,
        warnings=warnings,
    )


# ==========================================================================
# 多档位一次性采样（SOLUTION.md §4.2.3 Step 4 的硬性要求）
#
# 为什么要这个：5/10/15 分钟三条圈的【采样点完全可以复用】——
# 只要按最大档位（15 min）确定 r_max 采一次，同一份耗时场可以抽出任意阈值的等值线。
# 若每档各采一次，配额会变成 3 倍（1296 条路线 vs 432 条），
# 且 directionlite 兜底模式下耗时从 145 秒涨到 435 秒。
# ==========================================================================

@dataclass
class FieldBundle:
    """一次采样 + 一次 IDW 重建得到的可复用耗时场。"""

    center: tuple[float, float]
    xs: np.ndarray
    ys: np.ndarray
    z: np.ndarray
    mask: np.ndarray | None
    samples: list[RaySample]
    routes_charged: int
    n_requests: int
    monotonicity_modified_ratio: float
    r_max: float


def sample_field(
    provider,
    center_latlng: tuple[float, float],
    max_target_s: float = 900.0,
    cfg: SampleConfig | None = None,
    barriers_latlng: list[list[tuple[float, float]]] | None = None,
) -> FieldBundle:
    """按【最大档位】采一次样，重建出可服务所有阈值的耗时场。"""
    cfg = cfg or SampleConfig()
    proj = LocalProjection(*center_latlng)

    samples = ray_layout(cfg, max_target_s)
    charged = measure(provider, center_latlng, samples, proj)
    n_requests = math.ceil(len(samples) / MAX_ROUTES_PER_REQUEST)

    # 加密点用最大阈值定位（对更小的阈值同样落在合理区间内）
    crossings, mono_ratio = ray_roots(samples, max_target_s, cfg.rays)
    extra = densify(samples, crossings, cfg, max_target_s)
    if extra:
        charged += measure(provider, center_latlng, extra, proj)
        n_requests += math.ceil(len(extra) / MAX_ROUTES_PER_REQUEST)
    all_samples = samples + extra

    valid = [s for s in all_samples if s.t_s is not None]
    if len(valid) < 6:
        raise ValueError(f"有效采样点不足（{len(valid)} 个），无法重建耗时场")

    r_max = cfg.r_max(max_target_s)
    xs, ys = build_grid(-r_max * 1.2, r_max * 1.2, -r_max * 1.2, r_max * 1.2, cfg.grid_res)

    barriers_xy = []
    for ring in barriers_latlng or []:
        barriers_xy.append(np.array([proj.to_xy(la, ln) for la, ln in ring]))
    mask = make_barrier_mask(xs, ys, barriers_xy) if barriers_xy else None

    z = idw_interpolate(
        np.array([[s.x, s.y] for s in valid]),
        np.array([s.t_s for s in valid], dtype=float),
        xs, ys,
        k=cfg.idw_k, power=cfg.idw_power,
        center_fill=(0.0, 0.0),
        center_radius=cfg.dr * 0.6,
        center_speed=cfg.walk_speed,
    )

    return FieldBundle(
        center=center_latlng, xs=xs, ys=ys, z=z, mask=mask, samples=all_samples,
        routes_charged=charged, n_requests=n_requests,
        monotonicity_modified_ratio=mono_ratio, r_max=r_max,
    )


def _geom_to_xy(geom) -> list[tuple[float, float]]:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "MultiPolygon" and geom.geoms:
        geom = max(geom.geoms, key=lambda g: g.area)
    if geom.geom_type != "Polygon":
        return []
    return [(float(x), float(y)) for x, y in geom.exterior.coords]


def extract_from_field(bundle: FieldBundle, target_s: float):
    """从已有耗时场抽某个阈值的等时圈（零额外 API 调用）。"""
    return contour_to_polygon(bundle.xs, bundle.ys, bundle.z, bundle.mask, target_s)


def build_isochrones_multi(
    provider,
    center_latlng: tuple[float, float],
    targets_s: tuple[float, ...] = (300.0, 600.0, 900.0),
    cfg: SampleConfig | None = None,
    barriers_latlng: list[list[tuple[float, float]]] | None = None,
) -> dict[float, IsochroneResult]:
    """一次采样，产出多个时间档位的等时圈。

    配额成本 = 最大档位所需采样量（与档位数无关）。
    """
    cfg = cfg or SampleConfig()
    bundle = sample_field(provider, center_latlng, max(targets_s), cfg, barriers_latlng)

    out: dict[float, IsochroneResult] = {}
    for t in sorted(targets_s):
        geom = extract_from_field(bundle, t)
        poly = _geom_to_xy(geom)
        warnings: list[str] = []
        if not poly:
            warnings.append(f"{t:.0f}s 档位等值线提取为空")
        out[t] = IsochroneResult(
            polygon_xy=poly,
            area_m2=float(geom.area) if geom is not None and not geom.is_empty else 0.0,
            n_samples=len(bundle.samples),
            n_requests=bundle.n_requests,
            routes_charged=bundle.routes_charged,
            monotonicity_modified_ratio=bundle.monotonicity_modified_ratio,
            uncertainty_band_ratio=None,
            warnings=warnings,
        )
    return out
