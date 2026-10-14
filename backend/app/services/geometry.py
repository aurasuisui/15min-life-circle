"""几何工具：GeoJSON 转换、点面判断、连通域聚合。

约定（CONTRACT §4.1）：
  * 坐标系一律 **BD09**；
  * 坐标顺序一律 **[lng, lat]**；
  * 多边形**必须闭合**（首尾点相同）；
  * 面积一律在**局部 ENU 平面**上算（米），不用度数算面积。
"""
from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np
from shapely import make_valid
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

from ..core.geo import LocalProjection

EARTH_RADIUS_M = 6371008.8


def largest_polygon(geom):
    """取几何中【面积最大】的多边形。核心层的 IsochroneResult 只保留首个环，
    所以审计层必须自己取最大部件，而不是盲信 polygon_xy。"""
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "Polygon":
        return geom
    if geom.geom_type == "MultiPolygon" and geom.geoms:
        return max(geom.geoms, key=lambda g: g.area)
    parts = [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon" and not g.is_empty]
    return max(parts, key=lambda g: g.area) if parts else None


def geom_area_m2(geom) -> float:
    poly = largest_polygon(geom)
    return float(poly.area) if poly is not None else 0.0


def geom_to_lnglat_rings(geom, proj: LocalProjection, max_rings: int = 12) -> list[list[tuple[float, float]]]:
    """shapely 几何（局部 ENU 平面）-> [[(lng, lat), ...], ...]（外环 + 洞），BD09。"""
    poly = largest_polygon(geom)
    if poly is None:
        return []
    rings: list[list[tuple[float, float]]] = []

    def conv(ring):
        return [tuple(reversed(proj.to_latlng(x, y))) for x, y in ring.coords]  # (lng, lat)

    ext = conv(poly.exterior)
    if len(ext) >= 4:
        rings.append(ext)
    for interior in list(poly.interiors)[: max_rings - 1]:
        r = conv(interior)
        if len(r) >= 4:
            rings.append(r)
    return rings


def close_ring_lnglat(ring: list[tuple[float, float]]) -> list[list[float]]:
    out = [[float(a), float(b)] for a, b in ring]
    if out and out[0] != out[-1]:
        out.append(list(out[0]))
    return out


def to_geojson(rings: list[list[tuple[float, float]]]) -> dict[str, Any]:
    """[(lng, lat)...] 环列表 -> GeoJSON Polygon（自动闭合）。空输入返回退化三角形占位。"""
    coords = [close_ring_lnglat(r) for r in rings if len(r) >= 3]
    if not coords:
        coords = [[[0.0, 0.0], [0.0, 0.0], [0.0, 0.0]]]
    return {"type": "Polygon", "coordinates": coords}


def empty_geojson() -> dict[str, Any]:
    return {"type": "Polygon", "coordinates": [[[0.0, 0.0], [0.0, 0.0], [0.0, 0.0]]]}


def geometry_stats(geom) -> dict:
    if geom is None or geom.is_empty:
        return {"n_parts": 0, "n_holes": 0}
    poly = largest_polygon(geom)
    parts = len(geom.geoms) if geom.geom_type == "MultiPolygon" else 1
    return {"n_parts": parts, "n_holes": len(poly.interiors) if poly is not None else 0}


def project_points(center: tuple[float, float], lnglat: Iterable[tuple[float, float]]) -> np.ndarray:
    """(lng, lat) -> 局部 ENU (x, y)。"""
    proj = LocalProjection(*center)
    return np.array([proj.to_xy(lat, lng) for lng, lat in lnglat], dtype=float)


def cells_to_polygons(
    xs: np.ndarray,
    ys: np.ndarray,
    mask: np.ndarray,
    cell_size: float,
    min_area_m2: float = 5000.0,
    join_buffer: float = 0.55,
    simplify_ratio: float = 0.12,
):
    """把布尔栅格聚合为连通域多边形（米制平面坐标）。

    `join_buffer × cell_size` 的缓冲只为消除 4 连通判定留下的锯齿缝隙：
    先 buffer(+δ) 再 buffer(−δ) 做一次形态学闭运算，δ 取半格左右。
    **不要**取太大（曾经的 0.72 × 0.55 ≈ 90 m 收缩会把 3 格的小盲区
    压成 5 个点的退化四边形，甚至面积归零），也**不要**做激进化简
    （`simplify_ratio` 取 0.12 格 ≈ 15 m，只去掉真正的共线冗余点）。
    """
    from shapely.geometry import box

    idx = np.argwhere(mask)
    if idx.size == 0:
        return []
    half = cell_size / 2.0
    boxes = [box(xs[j] - half, ys[i] - half, xs[j] + half, ys[i] + half) for i, j in idx]
    merged = unary_union([make_valid(b) for b in boxes])
    delta = cell_size * join_buffer
    closed = merged.buffer(delta, join_style=2).buffer(-delta, join_style=2)
    if not closed.is_empty:
        merged = closed
    if merged.is_empty:
        return []
    parts = list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]
    parts = [p for p in parts if p.area >= min_area_m2]
    out = []
    for p in sorted(parts, key=lambda g: -g.area):
        fixed = make_valid(p)
        if fixed.geom_type == "MultiPolygon":
            fixed = max(fixed.geoms, key=lambda g: g.area)
        if fixed.geom_type == "Polygon" and fixed.area > 0:
            out.append(fixed.simplify(cell_size * simplify_ratio, preserve_topology=True))
    return out


def geom_to_wkb_hex(geom) -> str:
    """几何 -> WKB hex 字符串。

    比在报告 dict 里塞 shapely 对象（不可 JSON 序列化）或 Pickle 字节（有安全与
    平台耦合问题）都干净；调用方用 `geom_from_wkb_hex` 还原。
    """
    if geom is None:
        return ""
    try:
        import shapely.wkb

        return shapely.wkb.dumps(geom, hex=True)
    except Exception:
        return ""


def geom_from_wkb_hex(value: str, default=None):
    if not value:
        return default
    try:
        import shapely.wkb

        return shapely.wkb.loads(value, hex=True)
    except Exception:
        return default


def geom_centroid_latlng(geom, proj: LocalProjection) -> tuple[float, float]:
    c = geom.centroid
    lat, lng = proj.to_latlng(c.x, c.y)
    return float(lat), float(lng)


def geodesic_area_m2(geom, proj: LocalProjection) -> float:
    """多边形面积（米²，平面近似）。geom 已是平面坐标，直接取 area。"""
    return float(geom.area)
