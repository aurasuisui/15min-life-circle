"""可达性场：解析耗时场 + 边界带真实算路校正（SOLUTION §4.4 的 S3 方案）。

问题本质：盲区就是「耗时场下 Voronoi 分配中超阈值的格网」。

    BS = { g ∈ G :  t(g, NearestFacility(g)) > T }

三套方案里我们走 **S3 预筛 + 关键类别精算**：

  1. **解析耗时场（零配额）**：t̂(x, f) = len(x, f) / v(bearing) + 固定开销；
     len 由 `AnalyticModel` 给出，**与 mock / 快照生成共用同一实现** ——
     这一点很关键：如果预筛用的模型与真值模型不一致，边界带会选错，
     真实算路就白花了（我们第一版就踩了这个坑：预筛说 750 s，真值 1590 s）。
  2. **加性加权 Voronoi**：t̂(x, f) 关于 ‖x − f‖ 单调 ⇒ 等价于按加权距离划分，
     直接 numpy 全网格 argmin（约 500 格 × 20 设施，毫秒级）。
  3. **不确定带**：t̂₁(g) ∈ [band_lo·T, band_hi·T] 的格网才做真实算路校正。
     用「区间」而不是「|t̂−T| < δ」是因为解析模型在**跨屏障**处会系统性低估
     1.5~2.0 倍；区间法一次覆盖，代价是带宽略大 —— 但每格网只需 1 条路线。
  4. **预算保护**：带内格网按「离阈值近」排序，只精算前 max_routes 个，
     其余保持解析估计并写进 notes（绝不静默降级）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from shapely import contains_xy

from ..core.geo import LocalProjection
from .geometry import largest_polygon


def _route_lengths(model, ox: np.ndarray | float, oy: np.ndarray | float,
                   fx: np.ndarray, fy: np.ndarray) -> np.ndarray:
    """站点 -> 设施的步行路网长度（米）。跨屏障时用「经最近桥」的折线长度替代直线。"""
    dx = np.asarray(fx) - np.asarray(ox)
    dy = np.asarray(fy) - np.asarray(oy)
    straight = np.hypot(dx, dy) * model.detour_factor
    for bar in model.barriers:
        top = bar.y_m + bar.half_width_m
        bot = bar.y_m - bar.half_width_m
        o_arr = np.asarray(oy, dtype=float)
        o_side = np.where(o_arr > top, 1, np.where(o_arr < bot, -1, 0))
        f_side = np.where(fy > top, 1, np.where(fy < bot, -1, 0))
        cross = (o_side != 0) & (f_side != 0) & (f_side == -o_side)
        if not np.any(cross):
            continue
        bx = bar.bridge_x_m
        detour = np.hypot(np.asarray(ox) - bx, o_arr - bar.y_m) + np.hypot(bx - fx, bar.y_m - fy)
        straight = np.where(cross, detour, straight)
    return straight


def pair_time_s(model, ox: np.ndarray | float, oy: np.ndarray | float,
                fx: np.ndarray, fy: np.ndarray) -> np.ndarray:
    """站点 -> 设施的解析耗时（秒）。"""
    length = _route_lengths(model, ox, oy, fx, fy)
    bearing = np.degrees(np.arctan2(np.asarray(fx) - np.asarray(ox), np.asarray(fy) - np.asarray(oy))) % 360.0
    speed = model.base_speed * (1.0 + model.anisotropy * np.cos(np.radians(bearing - model.pref_bearing_deg)))
    return length / speed + model.overhead_s


@dataclass
class AccessGrid:
    """规则格网 + 每格到各类最近设施的解析耗时与归属。"""

    xs: np.ndarray
    ys: np.ndarray
    gx: np.ndarray
    gy: np.ndarray
    inside: np.ndarray
    passable: np.ndarray
    t_min: dict[str, np.ndarray] = field(default_factory=dict)
    nearest_idx: dict[str, np.ndarray] = field(default_factory=dict)
    dist_min: dict[str, np.ndarray] = field(default_factory=dict)  # 解析路网距离（米），盲区按距离判定

    @property
    def shape(self) -> tuple[int, int]:
        return self.gx.shape

    def valid(self) -> np.ndarray:
        return self.inside & self.passable


def _axis(lo: float, hi: float, cell: float, pad_cells: int, anchored: bool):
    """生成栅格轴。

    anchored=True 时把刻度对齐到 cell 的整数倍（以中心点为原点），
    这样格点会落在 0, ±125, ±250, ±375 … 上。

    为什么必须对齐：屏障（河道）是 90 m 宽的窄带，若格点全部落在带外，
    `passable` 掩膜会一个格点都盖不到，河道会被当成可通行区域。
    对齐全覆盖后，-250 m 这条格线正好落在河道中心线上。
    """
    if anchored:
        lo_i = int(math.floor(lo / cell)) - pad_cells
        hi_i = int(math.ceil(hi / cell)) + pad_cells
        return np.arange(lo_i, hi_i + 1, dtype=float) * cell
    n = max(2, int(math.ceil((hi - lo) / cell)) + 1 + 2 * pad_cells)
    return lo - pad_cells * cell + np.arange(n) * cell


def build_access_grid(
    model,
    center: tuple[float, float],
    domain_geom,
    facilities_xy: dict[str, np.ndarray],
    cell_size: float,
    pad_cells: int = 2,
    anchored: bool = True,
) -> AccessGrid:
    """在等时圈外接矩形（外扩 pad_cells）上建格网，并预计算每类设施的解析最近耗时。"""
    if domain_geom is None or domain_geom.is_empty:
        raise ValueError("研究域为空，无法建可达性格网")
    poly = largest_polygon(domain_geom)
    minx, miny, maxx, maxy = poly.bounds
    xs = _axis(minx, maxx, cell_size, pad_cells, anchored)
    ys = _axis(miny, maxy, cell_size, pad_cells, anchored)
    gx, gy = np.meshgrid(xs, ys)

    inside = contains_xy(poly, gx, gy)
    passable = np.ones(gx.shape, dtype=bool)
    for bar in model.barriers:
        passable &= ~((gy >= bar.y_m - bar.half_width_m) & (gy <= bar.y_m + bar.half_width_m))

    grid = AccessGrid(xs=xs, ys=ys, gx=gx, gy=gy, inside=inside, passable=passable)
    px, py = gx.ravel(), gy.ravel()

    for key, fxy in facilities_xy.items():
        if fxy is None or len(fxy) == 0:
            grid.t_min[key] = np.full(gx.shape, np.inf)
            grid.nearest_idx[key] = np.full(gx.shape, -1, dtype=int)
            continue
        t = np.full(px.shape, np.inf)
        d = np.full(px.shape, np.inf)
        idx = np.full(px.shape, -1, dtype=int)
        for i in range(len(fxy)):
            dd = _route_lengths(model, px, py, np.full_like(px, fxy[i, 0]), np.full_like(px, fxy[i, 1]))
            tt = pair_time_s(model, px, py, np.full_like(px, fxy[i, 0]), np.full_like(px, fxy[i, 1]))
            better = tt < t
            t = np.where(better, tt, t)
            d = np.where(better, dd, d)
            idx = np.where(better, i, idx)
        grid.t_min[key] = t.reshape(gx.shape)
        grid.dist_min[key] = d.reshape(gx.shape)
        grid.nearest_idx[key] = idx.reshape(gx.shape)
    return grid


def correction_band(grid: AccessGrid, key: str, threshold_m: float,
                    band_lo: float = 0.70, band_hi: float = 1.60) -> np.ndarray:
    """不确定带：解析**路网距离**落在 [band_lo·D, band_hi·D] 内的可通行域内格网。

    ⚠️ 这里用「距离」而不是「耗时」做判定依据，因为任务书 FR-4 的原文口径就是
    「周边 1 公里内没有……」。解析模型里的固定开销（红绿灯 20 s）会把纯距离口径
    带偏 20~130 m，用距离判定可以完全避开这个偏移。
    """
    d = grid.dist_min.get(key)
    if d is None:
        return np.zeros(grid.gx.shape, dtype=bool)
    return grid.valid() & np.isfinite(d) & (d >= band_lo * threshold_m) & (d <= band_hi * threshold_m)


@dataclass
class CorrectionResult:
    t_real: dict[str, np.ndarray]
    cells_total: int = 0
    cells_corrected: int = 0
    routes_charged: int = 0
    notes: list[str] = field(default_factory=list)


def correct_with_routes(
    grid: AccessGrid,
    keys: tuple[str, ...],
    facilities_xy: dict[str, np.ndarray],
    route_provider,
    center: tuple[float, float],
    threshold_m: float,
    max_routes: int = 240,
    chunk: int = 50,
    band_lo: float = 0.70,
    band_hi: float = 1.60,
) -> CorrectionResult:
    """对不确定带做真实算路校正。

    ⚠️ 只对「该格网解析归属的那个设施」算路（1 格 = 1 条路线），
    而不是对全部设施都算一遍（那会变成 1 格 = n 条路线，配额瞬间翻 n 倍）。
    """
    proj = LocalProjection(*center)
    t_real: dict[str, np.ndarray] = {}
    notes: list[str] = []
    charged = 0
    total_band = 0
    corrected = 0

    if route_provider is None:
        return CorrectionResult({}, 0, 0, 0, ["无算路 provider，盲区完全依赖解析模型"])

    for key in keys:
        t = grid.t_min.get(key)
        if t is None:
            continue
        t_real[key] = np.full(grid.shape, np.inf)
        band = correction_band(grid, key, threshold_m, band_lo, band_hi)
        cand = np.flatnonzero(band.ravel())
        total_band += cand.size
        if cand.size == 0:
            continue
        flat_d = grid.dist_min[key].ravel()
        order = cand[np.argsort(np.abs(flat_d[cand] - threshold_m))]
        if order.size > max_routes:
            notes.append(
                f"{key}：不确定带 {order.size} 格超过单类预算 {max_routes}，"
                f"只精算最靠近 {threshold_m:.0f} m 阈值的 {max_routes} 格，其余保留解析估计"
            )
            order = order[:max_routes]

        px, py = grid.gx.ravel()[order], grid.gy.ravel()[order]
        owner = grid.nearest_idx[key].ravel()[order]
        lnglat = [proj.to_latlng(float(x), float(y)) for x, y in zip(px, py)]
        out_flat = t_real[key].ravel()

        for i in np.unique(owner):
            if i < 0 or i >= len(facilities_xy[key]):
                continue
            sel = np.flatnonzero(owner == i)
            origin = proj.to_latlng(float(facilities_xy[key][i][0]), float(facilities_xy[key][i][1]))
            for start in range(0, len(sel), chunk):
                part = sel[start : start + chunk]
                dests = [lnglat[j] for j in part]
                try:
                    durs = route_provider.walking_matrix(origin, dests)
                except Exception as e:
                    notes.append(f"{key} 真实算路失败（{type(e).__name__}）：{e}")
                    continue
                charged += len(dests)
                for k, d in zip(part, durs):
                    if d not in (None, 0, 0.0):
                        out_flat[order[k]] = float(d)
                        corrected += 1
        t_real[key] = out_flat.reshape(grid.shape)

    if total_band:
        n_valid = int(grid.valid().sum()) or 1
        notes.append(
            f"盲区预筛：{total_band} 个格网进入不确定带（占域内可通行格网 {total_band / n_valid:.1%}），"
            f"其中 {corrected} 个完成真实算路校正，消耗 {charged} 条路线；"
            f"解析预筛本身零配额，只对边界带动用真实算路"
        )
    return CorrectionResult(t_real, total_band, corrected, charged, notes)


def effective_time(grid: AccessGrid, key: str, t_real: np.ndarray | None) -> np.ndarray:
    """真实耗时优先，其余用解析耗时回填。"""
    t = grid.t_min.get(key, np.full(grid.shape, np.inf))
    if t_real is None:
        return t
    return np.where(np.isfinite(t_real), t_real, t)


def effective_distance(grid: AccessGrid, key: str, t_real: np.ndarray | None,
                       model) -> np.ndarray:
    """真实耗时 -> 等效路网距离（米）；未校正处用解析距离回填。

    这样「盲区 = 距离 > 1000 m」与任务书原文口径完全一致，
    而真实算路的结果（时间）也能参与同一套距离判定。
    """
    d = grid.dist_min.get(key, np.full(grid.shape, np.inf))
    if t_real is None:
        return d
    v_eff = max(1e-6, model.effective_speed())
    d_real = (t_real - model.overhead_s) * v_eff
    return np.where(np.isfinite(t_real), d_real, d)


def blind_mask(grid: AccessGrid, key: str, threshold_m: float,
               t_real: np.ndarray | None = None, model=None) -> np.ndarray:
    """最终盲区掩膜（仅域内可通行格网），按【步行距离】判定。"""
    if model is None:
        from ..providers.barriers import zhangjiang_model

        model = zhangjiang_model()
    return grid.valid() & (effective_distance(grid, key, t_real, model) > threshold_m)
