"""内部数据模型（provider / 服务层之间传递的轻量结构）。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RouteRequest:
    """一次批量算路请求的规范化描述（用于快照录制与回放）。"""

    origin: tuple[float, float]
    destinations: list[tuple[float, float]]


@dataclass
class RouteResult:
    """算路结果 + 元信息。provider 协议要求返回 durations，元信息挂在 last_meta 上。"""

    durations: list[float | None]
    source: str = "live"
    degraded: bool = False
    reasons: list[str] = field(default_factory=list)
    straight_distances: list[float | None] | None = None
    route_count: int = 0


@dataclass
class PlaceResult:
    """地点检索结果 + 元信息。"""

    query: str
    results: list[dict]
    source: str = "live"
    degraded: bool = False
    reasons: list[str] = field(default_factory=list)
    radius: int = 1500
    out_of_bounds_ratio: float = 0.0
