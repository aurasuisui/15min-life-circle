"""耗时场重建：IDW 插值到规则栅格 + 屏障遮蔽。

方案依据（SOLUTION.md §4.2.3 Step 5）：
Bielecka & Bober 2013 实测 IDW 的 MAPE/MAE 最低；样条(RBF/薄板)会产生
「最不合理的等时圈离岛」，最近邻误差最大。RBF 因全局稠密矩阵 O(N^3) 与过冲不采用。
"""
from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree


def build_grid(
    x_min: float, x_max: float, y_min: float, y_max: float, res: float
) -> tuple[np.ndarray, np.ndarray]:
    """生成规则栅格坐标轴。"""
    xs = np.arange(x_min, x_max + res * 0.5, res)
    ys = np.arange(y_min, y_max + res * 0.5, res)
    return xs, ys


def idw_interpolate(
    sample_xy: np.ndarray,
    sample_v: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    k: int = 8,
    power: float = 2.0,
    center_fill: tuple[float, float] | None = None,
    center_radius: float = 0.0,
    center_speed: float = 1.25,
) -> np.ndarray:
    """IDW(k, p) 插值到栅格。返回 shape=(len(ys), len(xs)) 的耗时场。

    center_fill: (x0, y0) 中心点平面坐标。半径 center_radius 内的圆盘无样本，
                 用步行速度锥 t = r / v 填充，避免中心空洞。
    """
    gx, gy = np.meshgrid(xs, ys)
    pts = np.column_stack([gx.ravel(), gy.ravel()])

    tree = cKDTree(sample_xy)
    kk = min(k, len(sample_xy))
    dist, idx = tree.query(pts, k=kk)
    if kk == 1:
        dist = dist[:, None]
        idx = idx[:, None]

    with np.errstate(divide="ignore", invalid="ignore"):
        w = 1.0 / np.power(dist, power)
    w[~np.isfinite(w)] = 1e12  # 与样本点重合
    vals = sample_v[idx]
    z = np.sum(w * vals, axis=1) / np.sum(w, axis=1)
    z = z.reshape(len(ys), len(xs))

    if center_fill is not None and center_radius > 0:
        cx, cy = center_fill
        r = np.hypot(gx - cx, gy - cy)
        cone = r / center_speed
        np.copyto(z, cone, where=(r < center_radius) & (cone < z))

    return z


def make_barrier_mask(
    xs: np.ndarray, ys: np.ndarray, barriers_xy: list[np.ndarray]
) -> np.ndarray:
    """构造屏障 mask（True = 不可通行，contourpy 会跳过）。

    屏障 = 水体 / 铁路 / 封闭小区 / 快速路等不可穿越地物。
    不做这一步，IDW 会把河对岸的低耗时拉过来，在河道上生成「穿墙」的假覆盖。
    """
    gx, gy = np.meshgrid(xs, ys)
    mask = np.zeros(gx.shape, dtype=bool)
    for poly in barriers_xy:
        if len(poly) < 3:
            continue
        mask |= _points_in_polygon(gx, gy, poly)
    return mask


def _points_in_polygon(
    gx: np.ndarray, gy: np.ndarray, poly: np.ndarray
) -> np.ndarray:
    """射线法点-in-多边形，向量化实现。poly: (N, 2)。"""
    x, y = gx.ravel(), gy.ravel()
    inside = np.zeros(x.shape, dtype=bool)
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        cond = ((yi > y) != (yj > y)) & (
            x < (xj - xi) * (y - yi) / np.where(yj - yi == 0, 1e-12, yj - yi) + xi
        )
        inside ^= cond
        j = i
    return inside.reshape(gx.shape)
