"""覆盖统计（FR-3）：数量 / 密度 / 最近一处步行耗时 / 覆盖率评分。

对应 CONTRACT §4 的 `facilities.categories[]`。

指标定义：
  count               圈内设施数量（本地裁剪后实测，**不用 total** —— total 封顶 150 且需 page_num）
  density_per_km2     count / 等时圈面积(km²)
  nearest_walk_seconds 中心点 -> 最近设施的**真实步行耗时**（批量算路），
                      预算不足时退化为解析估值并标注
  coverage_score      见下（0~1）

  access  = clip(1 − t_nearest / 900, 0, 1)
  present = 1 if count > 0 else 0
  density = clip(density / density_ref, 0, 1)
  score   = 0.45·access + 0.35·present + 0.20·density      （SOLUTION §4.5）
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..constants import (
    ACCESS_REF_SECONDS,
    ACCESS_W,
    CATEGORY_KEYS,
    DENSITY_REF_PER_KM2,
    DENSITY_W,
    FACILITY_CATEGORIES,
    PRESENT_W,
    WALK_SPEED_MPS,
)
from ..core.geo import haversine_m
from .poi import PoiRecord

MAX_ROUTES = 50


@dataclass
class CategoryStat:
    key: str
    name: str
    query: str
    count: int
    density_per_km2: float
    nearest_walk_seconds: int | None
    coverage_score: float
    pois: list[dict] = field(default_factory=list)
    nearest_estimated: bool = False


@dataclass
class CoverageResult:
    stats: list[CategoryStat]
    routes_charged: int = 0
    notes: list[str] = field(default_factory=list)

    def by_key(self) -> dict[str, CategoryStat]:
        return {s.key: s for s in self.stats}


def coverage_score(count: int, density: float, nearest_s: float | None) -> float:
    access = 0.0 if nearest_s is None else max(0.0, min(1.0, 1.0 - nearest_s / ACCESS_REF_SECONDS))
    present = 1.0 if count > 0 else 0.0

    def density_term(key: str) -> float:
        ref = DENSITY_REF_PER_KM2.get(key, 10.0)
        return max(0.0, min(1.0, density / ref)) if ref > 0 else 0.0

    return ACCESS_W * access + PRESENT_W * present  # 密度项在下面按类别加权


def _density_term(key: str, density: float) -> float:
    ref = DENSITY_REF_PER_KM2.get(key, 10.0)
    return max(0.0, min(1.0, density / ref)) if ref > 0 else 0.0


def _score(key: str, count: int, density: float, nearest_s: float | None) -> float:
    access = 0.0
    if nearest_s is not None:
        access = max(0.0, min(1.0, 1.0 - nearest_s / ACCESS_REF_SECONDS))
    present = 1.0 if count > 0 else 0.0
    return ACCESS_W * access + PRESENT_W * present + DENSITY_W * _density_term(key, density)


def compute_coverage(
    records: list[PoiRecord],
    area_m2: float,
    center: tuple[float, float],
    route_provider=None,
    walk_budget: int = 60,
    walk_seconds: dict[str, float] | None = None,
) -> CoverageResult:
    """统计 8 类设施的覆盖情况。

    真实步行耗时预算只花在「每类直线最近的若干个设施」上（默认每类 5 个），
    其余用解析估值，保证 live 模式下也不会因为一个社区烧掉上千条路线。
    """
    area_km2 = max(area_m2 / 1e6, 1e-9)
    notes: list[str] = []
    stats: list[CategoryStat] = []
    charged = 0
    walk_seconds = dict(walk_seconds or {})

    # 1) 每类按直线距离排序，选出需要真实算路的候选（分类配额，避免一类吃光预算）
    per_cat_quota = max(2, walk_budget // max(1, len(CATEGORY_KEYS)))
    candidates: list[PoiRecord] = []
    by_cat: dict[str, list[PoiRecord]] = {k: [] for k in CATEGORY_KEYS}
    for r in records:
        if r.category in by_cat:
            by_cat[r.category].append(r)
    for key in CATEGORY_KEYS:
        grp = sorted(by_cat[key], key=lambda r: haversine_m(center, (r.lat, r.lng)))
        candidates.extend(grp[:per_cat_quota])

    # 2) 真实算路（批量，1 起点 × N 终点）
    unresolved: list[PoiRecord] = []
    if route_provider is not None and candidates:
        for i in range(0, len(candidates), MAX_ROUTES):
            chunk = candidates[i : i + MAX_ROUTES]
            dests = [(r.lat, r.lng) for r in chunk]
            try:
                durs = route_provider.walking_matrix(center, dests)
                charged += len(chunk)
                for r, d in zip(chunk, durs):
                    key = r.uid or f"{r.name}@{r.lat:.5f},{r.lng:.5f}"
                    if d not in (None, 0, 0.0):
                        walk_seconds[key] = float(d)
                    else:
                        unresolved.append(r)
            except Exception as e:  # provider 失败不拖垮统计
                notes.append(f"覆盖统计的真实算路失败（{type(e).__name__}）：{e}；相关设施改用解析估值")
                unresolved.extend(chunk)

    # 3) 解析估值兜底
    estimated = 0
    for r in records:
        key = r.uid or f"{r.name}@{r.lat:.5f},{r.lng:.5f}"
        if key in walk_seconds:
            continue
        est = haversine_m(center, (r.lat, r.lng)) * 1.15 / WALK_SPEED_MPS + 20.0
        walk_seconds[key] = est
        estimated += 1
    if estimated:
        notes.append(
            f"{estimated} 个设施未做真实算路，步行耗时由解析模型给出"
            f"（t ≈ 直线距离×1.15 / {WALK_SPEED_MPS} m/s + 20 s）"
        )

    # 4) 汇总
    for key in CATEGORY_KEYS:
        grp = sorted(by_cat[key], key=lambda r: walk_seconds.get(
            r.uid or f"{r.name}@{r.lat:.5f},{r.lng:.5f}", 1e9))
        count = len(grp)
        density = count / area_km2
        nearest = None
        nearest_estimated = False
        if grp:
            r0 = grp[0]
            k0 = r0.uid or f"{r0.name}@{r0.lat:.5f},{r0.lng:.5f}"
            nearest = int(round(walk_seconds[k0]))
            nearest_estimated = r0 not in candidates
        stats.append(CategoryStat(
            key=key,
            name=str(FACILITY_CATEGORIES[key]["name"]),
            query=str(FACILITY_CATEGORIES[key]["query"]),
            count=count,
            density_per_km2=round(density, 2),
            nearest_walk_seconds=nearest,
            coverage_score=round(_score(key, count, density, nearest), 4),
            nearest_estimated=nearest_estimated,
            pois=[{
                "uid": r.uid, "name": r.name, "lat": round(r.lat, 6), "lng": round(r.lng, 6),
                "address": r.address, "tag": r.tag,
                "walk_seconds": int(round(walk_seconds.get(r.uid or f"{r.name}@{r.lat:.5f},{r.lng:.5f}", 0))),
            } for r in grp[:50]],
        ))
    return CoverageResult(stats=stats, routes_charged=charged, notes=notes)
