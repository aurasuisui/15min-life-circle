r"""等时圈重建方法对照实验（复现 docs/SOLUTION.md §4.2.2 的结论）。

合成场景：2.4 km x 2.4 km、25 m 栅格、一条不可穿越的河 + 唯一桥梁，
制造强凹陷与遮蔽阴影。真值 = 8 连通栅格 Dijkstra（步行 1.25 m/s）。

横向对比：
  A) 极坐标连点    —— 射线求根后把交点直接连成多边形（被否定的做法）
  B) IDW + 等值线  —— 本方案采用的做法

指标：与真值覆盖区的 IoU、面积相对误差。

用法： .venv\Scripts\python.exe bench\benchmark_isochrone.py
"""
from __future__ import annotations

import heapq
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.geo import LocalProjection, polygon_area_m2
from app.core.isochrone import SampleConfig, build_isochrone

GRID_RES = 25.0
HALF = 1600.0
CENTER = (31.2045, 121.5990)   # 张江海科路附近
SPEED = 1.25
TARGET_S = 900.0
RIVER_Y, RIVER_HALF_W = 400.0, 40.0
BRIDGE_HALF_W = 60.0


# ---------------------------------------------------------------- 真值场
def build_truth():
    n = int(2 * HALF / GRID_RES) + 1
    xs = np.linspace(-HALF, HALF, n)
    ys = np.linspace(-HALF, HALF, n)
    passable = np.ones((n, n), dtype=bool)
    for j, y in enumerate(ys):
        if abs(y - RIVER_Y) <= RIVER_HALF_W:
            for i, x in enumerate(xs):
                if abs(x) > BRIDGE_HALF_W:
                    passable[j, i] = False

    INF = float("inf")
    t = np.full((n, n), INF)
    ci = int(np.argmin(np.abs(xs)))
    cj = int(np.argmin(np.abs(ys)))
    t[cj, ci] = 0.0
    pq = [(0.0, cj, ci)]
    nbrs = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
            (1, 1, math.sqrt(2)), (1, -1, math.sqrt(2)), (-1, 1, math.sqrt(2)), (-1, -1, math.sqrt(2))]
    while pq:
        d, j, i = heapq.heappop(pq)
        if d > t[j, i]:
            continue
        for dj, di, w in nbrs:
            nj, ni = j + dj, i + di
            if 0 <= nj < n and 0 <= ni < n and passable[nj, ni]:
                nd = d + w * GRID_RES / SPEED
                if nd < t[nj, ni]:
                    t[nj, ni] = nd
                    heapq.heappush(pq, (nd, nj, ni))
    t[~passable] = INF
    return xs, ys, t, passable


class TruthProvider:
    """把真值场伪装成「百度算路 API」。"""

    def __init__(self, xs, ys, t, passable, proj: LocalProjection):
        self.xs, self.ys, self.t, self.passable, self.proj = xs, ys, t, passable, proj
        pj, pi = np.nonzero(passable)
        self._px, self._py = xs[pi], ys[pj]
        self._pv = t[pj, pi]

    def walking_matrix(self, origin_latlng, destinations):
        from scipy.spatial import cKDTree

        tree = cKDTree(np.column_stack([self._px, self._py]))
        xy = np.array([self.proj.to_xy(la, ln) for la, ln in destinations])
        _, idx = tree.query(xy, k=1)
        out = []
        for v in self._pv[idx]:
            out.append(None if not np.isfinite(v) else float(v))
        return out


# ---------------------------------------------------------------- 基线 A
def polar_polygon(xs, ys, t, passable, rays: int, step: float = 5.0):
    """每条射线找首个穿越点，直接连成多边形（被否定的做法）。"""
    proj_pts = []
    for i in range(rays):
        b = math.radians(360.0 * i / rays)
        r, hit = 0.0, None
        rmax = 2500.0
        while r < rmax:
            x, y = r * math.sin(b), r * math.cos(b)
            v = _sample(xs, ys, t, x, y)
            if v is None or v > TARGET_S:
                hit = r
                break
            r += step
        if hit is None:
            hit = rmax
        proj_pts.append((hit * math.sin(b), hit * math.cos(b)))
    return proj_pts


def _sample(xs, ys, t, x, y):
    """最近邻取真值。"""
    i = int(np.argmin(np.abs(xs - x)))
    j = int(np.argmin(np.abs(ys - y)))
    v = t[j, i]
    return None if not np.isfinite(v) else float(v)


# ---------------------------------------------------------------- IoU
def iou(geom, xs, ys, truth_mask):
    import shapely

    gx, gy = np.meshgrid(xs, ys)
    inside = shapely.contains_xy(geom, gx.ravel(), gy.ravel()).reshape(gx.shape)
    inter = np.logical_and(inside, truth_mask).sum()
    union = np.logical_or(inside, truth_mask).sum()
    return inter / union if union else 0.0


def area_err(geom, truth_mask, res=GRID_RES):
    return (geom.area - truth_mask.sum() * res * res) / (truth_mask.sum() * res * res)


# ---------------------------------------------------------------- main
def main():
    from shapely.geometry import Polygon

    xs, ys, t, passable = build_truth()
    proj = LocalProjection(*CENTER)
    truth_mask = t <= TARGET_S
    print(f"真值：覆盖格数={truth_mask.sum()}  面积={truth_mask.sum()*GRID_RES**2/1e6:.3f} km^2")
    print(f"不可通行格数={(~passable).sum()}\n")

    provider = TruthProvider(xs, ys, t, passable, proj)

    configs = [
        ("R=36 dr=150", SampleConfig(rays=36, dr=150.0, densify_per_crossing=0)),
        ("R=36 dr=200 +加密3", SampleConfig(rays=36, dr=200.0, densify_per_crossing=3)),
        ("R=36 dr=100", SampleConfig(rays=36, dr=100.0, densify_per_crossing=0)),
    ]

    print(f"{'配置':<22}{'点数':>6}{'请求':>5}{'极坐标IoU':>11}{'IDW IoU':>10}{'IDW面积误差':>12}")
    print("-" * 70)

    # 屏障（河）多边形：必须【排除桥洞】，否则把唯一通道也屏蔽掉，
    # 会导致河北岸可达区整体丢失（实测面积低估 21%）。
    def _band(x0, x1):
        return [
            proj.to_latlng(x0, RIVER_Y - RIVER_HALF_W),
            proj.to_latlng(x1, RIVER_Y - RIVER_HALF_W),
            proj.to_latlng(x1, RIVER_Y + RIVER_HALF_W),
            proj.to_latlng(x0, RIVER_Y + RIVER_HALF_W),
        ]

    river_bands = [_band(-HALF, -BRIDGE_HALF_W), _band(BRIDGE_HALF_W, HALF)]

    for name, cfg in configs:
        ppts = polar_polygon(xs, ys, t, passable, cfg.rays)
        pg = Polygon(ppts).buffer(0)
        p_iou = iou(pg, xs, ys, truth_mask)

        res = build_isochrone(provider, CENTER, TARGET_S, cfg, barriers_latlng=river_bands)
        if res.polygon_xy:
            g = Polygon(res.polygon_xy)
            if not g.is_valid:
                from shapely import make_valid
                g = make_valid(g)
            d_iou = iou(g, xs, ys, truth_mask)
            d_err = area_err(g, truth_mask)
        else:
            d_iou, d_err = 0.0, 0.0

        print(f"{name:<22}{res.n_samples:>6}{res.n_requests:>5}{p_iou:>11.3f}{d_iou:>10.3f}{d_err:>11.1%}")

    print("\n结论对照（docs/SOLUTION.md §4.2.2）：")
    print("  * 极坐标连点系统性低估面积（凹角被切掉），IoU 约 0.90~0.93")
    print("  * IDW+等值线用更少采样达到更高 IoU（约 0.94~0.96）")


if __name__ == "__main__":
    import io

    buf = io.StringIO()
    _stdout = sys.stdout
    sys.stdout = buf
    try:
        main()
    finally:
        sys.stdout = _stdout
        out = Path(__file__).resolve().parent / "result.txt"
        out.write_text(buf.getvalue(), encoding="utf-8")
        print(f"written -> {out}")
