"""8 维评分模型（FR-5 雷达图 + 综合分，SOLUTION §4.5）。

    access_c  = 100 · clip(1 − t_nearest_c / 900, 0, 1)      最近一处步行可达性
    present_c = 100 if count_c > 0 else 0                     有无
    density_c = 100 · clip(density_c / density_ref_c, 0, 1)   相对规范参考密度

    score_c   = 0.45·access_c + 0.35·present_c + 0.20·density_c
    综合分    = Σ w_d · score_{d 内各类的加权平均}
    等级      ≥85 A / 70–85 B / 55–70 C / 40–55 D / <40 D

`density_ref_c` 见 `app/constants.py`（依据 GB 50180-2018 与《完整居住社区建设标准（试行）》折算）。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..constants import (
    ACCESS_REF_SECONDS,
    ACCESS_W,
    DENSITY_REF_PER_KM2,
    DENSITY_W,
    DIMENSION_CATEGORIES,
    DIMENSION_NAMES,
    GRADE_BANDS,
    PRESENT_W,
    SCORE_WEIGHTS,
)
from .coverage import CategoryStat


@dataclass
class DimensionScore:
    key: str
    name: str
    score: float
    weight: float
    raw_count: int


@dataclass
class ScoreResult:
    total: float
    grade: str
    dimensions: list[DimensionScore]


def grade_of(total: float) -> str:
    for band, letter in GRADE_BANDS:
        if total >= band:
            return letter
    return "D"


def _cat_score(stat: CategoryStat) -> float:
    access = 0.0
    if stat.nearest_walk_seconds is not None:
        access = max(0.0, min(1.0, 1.0 - stat.nearest_walk_seconds / ACCESS_REF_SECONDS))
    present = 1.0 if stat.count > 0 else 0.0
    ref = DENSITY_REF_PER_KM2.get(stat.key, 10.0)
    density = max(0.0, min(1.0, stat.density_per_km2 / ref)) if ref > 0 else 0.0
    return 100.0 * (ACCESS_W * access + PRESENT_W * present + DENSITY_W * density)


def compute_score(stats: list[CategoryStat]) -> ScoreResult:
    by_key = {s.key: s for s in stats}
    dims: list[DimensionScore] = []
    for dim_key, cats in DIMENSION_CATEGORIES.items():
        subs = [by_key[c] for c in cats if c in by_key]
        if not subs:
            continue
        score = sum(_cat_score(s) for s in subs) / len(subs)
        dims.append(DimensionScore(
            key=dim_key,
            name=DIMENSION_NAMES.get(dim_key, dim_key),
            score=round(score, 2),
            weight=SCORE_WEIGHTS.get(dim_key, 0.0),
            raw_count=sum(s.count for s in subs),
        ))
    wsum = sum(d.weight for d in dims) or 1.0
    total = sum(d.score * d.weight for d in dims) / wsum
    return ScoreResult(total=round(total, 2), grade=grade_of(total), dimensions=dims)
