"""体检报告 Pydantic 模型 —— **严格对齐 CONTRACT.md §4**，字段名不得更改。

    GET /api/audit 返回此结构。前端、AI 层、文档全部对齐它。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Priority = Literal["high", "medium", "low"]


class LatLng(BaseModel):
    lat: float
    lng: float


class GeoJSONPolygon(BaseModel):
    """GeoJSON 多边形。坐标一律 BD09，顺序一律 [lng, lat]，**必须闭合**。"""

    type: Literal["Polygon"] = "Polygon"
    coordinates: list[list[list[float]]]


class MetaQuota(BaseModel):
    routes_used: int = 0
    routes_limit: int = 5000
    poi_used: int = 0
    poi_limit: int = 100


class Meta(BaseModel):
    center: LatLng
    address: str = ""
    generated_at: str
    data_mode: Literal["live", "replay", "hybrid", "mock"] = "mock"
    degraded: bool = False
    degrade_reasons: list[str] = Field(default_factory=list)
    quota: MetaQuota = Field(default_factory=MetaQuota)


class Isochrone(BaseModel):
    target_seconds: float
    area_m2: float
    n_samples: int
    routes_charged: int
    uncertainty_band_ratio: float | None = None
    geometry: GeoJSONPolygon


class Poi(BaseModel):
    uid: str = ""
    name: str
    lat: float
    lng: float
    address: str = ""
    tag: str = ""
    walk_seconds: int | None = None


class FacilityCategory(BaseModel):
    key: str
    name: str
    query: str
    count: int
    density_per_km2: float
    nearest_walk_seconds: int | None = None
    coverage_score: float
    pois: list[Poi] = Field(default_factory=list)


class CleaningFunnel(BaseModel):
    raw: int = 0
    deduped: int = 0
    corrected: int = 0
    inside: int = 0


class Facilities(BaseModel):
    categories: list[FacilityCategory] = Field(default_factory=list)
    cleaning_funnel: CleaningFunnel = Field(default_factory=CleaningFunnel)


class BlindSpot(BaseModel):
    area_m2: float
    missing: list[str] = Field(default_factory=list)
    sample_point: LatLng
    geometry: GeoJSONPolygon


class BlindSpots(BaseModel):
    total_area_m2: float = 0.0
    polygons: list[BlindSpot] = Field(default_factory=list)


class ScoreDimension(BaseModel):
    key: str
    name: str
    score: float
    weight: float
    raw_count: int


class Score(BaseModel):
    total: float
    grade: Literal["A", "B", "C", "D"]
    dimensions: list[ScoreDimension] = Field(default_factory=list)


class Siting(BaseModel):
    lat: float
    lng: float
    reason: str
    gains: dict[str, int] = Field(default_factory=dict)


class AiEvidence(BaseModel):
    clause: str
    source: str = ""


class AiSuggestion(BaseModel):
    text: str
    priority: Priority = "medium"


class AiReport(BaseModel):
    degraded: bool = True
    diagnosis: str = ""
    evidence: list[AiEvidence] = Field(default_factory=list)
    suggestions: list[AiSuggestion] = Field(default_factory=list)


class AuditReport(BaseModel):
    meta: Meta
    isochrones: dict[str, Isochrone] = Field(default_factory=dict)
    facilities: Facilities = Field(default_factory=Facilities)
    blindspots: BlindSpots = Field(default_factory=BlindSpots)
    score: Score
    siting: list[Siting] = Field(default_factory=list)
    ai_report: AiReport = Field(default_factory=AiReport)


# --------------------------------------------------------------------------
# 几何工具
# --------------------------------------------------------------------------
def close_ring(ring: list[list[float]]) -> list[list[float]]:
    """保证多边形闭合（首尾点相同，CONTRACT §4.1）。"""
    if not ring:
        return ring
    out = [[float(p[0]), float(p[1])] for p in ring]
    if out[0] != out[-1]:
        out.append(list(out[0]))
    return out


def to_geojson_polygon(rings_lnglat: list[list[tuple[float, float]]]) -> dict[str, Any]:
    """把 [(lng, lat), ...] 的环列表转成 GeoJSON Polygon dict（自动闭合）。"""
    coordinates = [close_ring([[lng, lat] for lng, lat in ring]) for ring in rings_lnglat if len(ring) >= 3]
    return {"type": "Polygon", "coordinates": coordinates or [[[0.0, 0.0], [0.0, 0.0], [0.0, 0.0]]]}


def empty_polygon() -> dict[str, Any]:
    return {"type": "Polygon", "coordinates": [[[0.0, 0.0], [0.0, 0.0], [0.0, 0.0]]]}
