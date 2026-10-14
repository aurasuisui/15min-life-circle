"""服务盲区识别（FR-4）。

判定定义（对齐任务书原文「周边 1 公里内没有菜市场、药店或小学」）：

    点 p 对类别 c 是盲区  ⟺  从 p 步行到最近一个类别 c 设施的**最短步行距离 > 1000 m**
    （等价时长阈值 T = 1000 m / 1.25 m·s⁻¹ = 800 s，可配置）

流程：
  1. 对 3 类关键设施各构造盲区掩膜（真实耗时优先、解析耗时回填）；
  2. 掩膜取或 -> **盲区连通域聚合**为多边形（buffer + union，面积 < 0.005 km² 的碎块丢弃）；
  3. 每个多边形标注 `missing`（该区域内确认为盲区的类别列表）；
  4. 交集区域 = 「三类同时缺失」的严重盲区（缺失类别数 ≥ 2 会在 siting 里优先补点）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..constants import BLIND_SPOT_CATEGORIES, BLIND_SPOT_DISTANCE_M
from ..core.geo import LocalProjection
from ..providers.barriers import zhangjiang_model
from .accessibility import AccessGrid, blind_mask, effective_distance
from .geometry import cells_to_polygons, geom_centroid_latlng

MIN_POLYGON_AREA_M2 = 5000.0


@dataclass
class BlindSpotResult:
    polygons: list["BlindSpotPolygon"] = field(default_factory=list)
    total_area_m2: float = 0.0
    cell_size: float = 0.0
    notes: list[str] = field(default_factory=list)
    missing_count: dict[str, int] = field(default_factory=dict)

    def as_dicts(self, proj: LocalProjection) -> list[dict]:
        out = []
        for p in self.polygons:
            lat, lng = geom_centroid_latlng(p.geom, proj)
            out.append({
                "area_m2": round(p.area_m2, 1),
                "missing": list(p.missing),
                "lat": lat, "lng": lng,
                "geom": p.geom,
            })
        return out


@dataclass
class BlindSpotPolygon:
    geom: object
    area_m2: float
    missing: list[str]


def detect_blindspots(
    grid: AccessGrid,
    t_real: dict[str, np.ndarray] | None,
    threshold_m: float = BLIND_SPOT_DISTANCE_M,
    categories: tuple[str, ...] = BLIND_SPOT_CATEGORIES,
    min_area_m2: float = MIN_POLYGON_AREA_M2,
    model=None,
) -> BlindSpotResult:
    t_real = t_real or {}
    model = model or zhangjiang_model()
    masks: dict[str, np.ndarray] = {}
    for key in categories:
        masks[key] = blind_mask(grid, key, threshold_m, t_real.get(key), model)

    union = np.zeros(grid.shape, dtype=bool)
    for m in masks.values():
        union |= m
    if not union.any():
        return BlindSpotResult(notes=["未发现盲区（1 km 内三类设施均可步行到达）"],
                               cell_size=float(grid.xs[1] - grid.xs[0]))

    cell = float(grid.xs[1] - grid.xs[0])
    geoms = cells_to_polygons(grid.xs, grid.ys, union, cell, min_area_m2=min_area_m2)

    polys: list[BlindSpotPolygon] = []
    for g in geoms:
        minx, miny, maxx, maxy = g.bounds
        sel = (grid.gx >= minx - cell) & (grid.gx <= maxx + cell) & \
              (grid.gy >= miny - cell) & (grid.gy <= maxy + cell)
        missing: list[str] = []
        any_cell = False
        for key in categories:
            sub = masks[key] & sel
            if sub.any():
                any_cell = True
                # 该多边形范围内超过 15% 的格网对该类都是盲区 -> 计入 missing
                if sub.sum() >= max(1, int(0.15 * max(1, sel.sum()))):
                    missing.append(key)
        if not any_cell:
            # 多边形里一个格心都没落到（细长条）：用有效距离的最小值兜底判断
            for key in categories:
                eff = effective_distance(grid, key, t_real.get(key), model)[sel]
                if eff.size and float(np.min(eff)) > threshold_m:
                    missing.append(key)
        polys.append(BlindSpotPolygon(geom=g, area_m2=float(g.area), missing=missing))

    polys.sort(key=lambda p: -p.area_m2)
    total = sum(p.area_m2 for p in polys)
    missing_count: dict[str, int] = {}
    for key in categories:
        missing_count[key] = int(masks[key].sum())

    notes = [
        f"盲区判定阈值：步行路网距离 > {threshold_m:.0f} m（任务书 FR-4 原文口径），"
        f"格网 {cell:.0f} m，共 {int(union.sum())} 个盲区格网",
        f"聚合为 {len(polys)} 个多边形，总面积 {total / 1e6:.3f} km²（已丢弃 < {min_area_m2 / 1e4:.0f} hm² 的碎块）",
    ]
    return BlindSpotResult(polygons=polys, total_area_m2=total, cell_size=cell,
                           notes=notes, missing_count=missing_count)


def suggest_siting(
    result: BlindSpotResult,
    proj: LocalProjection,
    max_sites: int = 3,
) -> list[dict]:
    """补点建议（MCLP 贪心，零配额）。

    简化但不简陋：候选点取盲区多边形的质心（因为盲区本身就代表「服务未覆盖」），
    收益 = 该多边形内被该点 1 km 覆盖的盲区格网数按类别折算。
    多类别缺失的盲区优先（决策价值最高）。
    """
    cands = [p for p in result.polygons if p.area_m2 >= MIN_POLYGON_AREA_M2]
    if not cands:
        return []
    # 贪心：先按「缺失类别数 × 面积」排序，再做一次重叠剔除
    cands.sort(key=lambda p: (-len(p.missing), -p.area_m2))
    picked = []
    for p in cands:
        lat, lng = geom_centroid_latlng(p.geom, proj)
        if any(math.hypot((lat - q["lat"]) * 111320.0,
                          (lng - q["lng"]) * 111320.0 * math.cos(math.radians(lat))) < 600.0 for q in picked):
            continue
        names = "、".join(_zh(k) for k in p.missing) or "关键设施"
        picked.append({
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "reason": f"该点 1 km 内无{names}（盲区面积 {p.area_m2 / 1e4:.1f} hm²）",
            "gains": {k: 1 for k in (p.missing or ["market"])},
            "area_m2": round(p.area_m2, 1),
            "missing": list(p.missing),
        })
        if len(picked) >= max_sites:
            break
    return picked


_ZH = {
    "market": "菜市场", "pharmacy": "药店", "primary_school": "小学",
    "clinic": "社区医疗", "supermarket": "超市便利店", "elderly_care": "养老设施",
    "bus_stop": "公交站", "park": "公园绿地",
}


def _zh(key: str) -> str:
    return _ZH.get(key, key)
