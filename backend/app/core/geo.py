"""局部 ENU 投影与距离工具。

为什么不用经纬度直接算：等时圈工作在 25 m 栅格尺度上，
经度一度的实际长度随纬度变化，直接用度数建栅格会产生各向异性畸变。
统一投影到以中心点为原点的局部 ENU 平面（米），算完再投回经纬度。
"""
from __future__ import annotations

import math

EARTH_RADIUS_M = 6371008.8


class LocalProjection:
    """以 (lat0, lng0) 为原点的局部东-北平面投影。

    x = 东向（米）, y = 北向（米）。
    """

    def __init__(self, lat0: float, lng0: float):
        self.lat0 = lat0
        self.lng0 = lng0
        self._m_per_deg_lat = math.radians(1.0) * EARTH_RADIUS_M
        self._m_per_deg_lng = self._m_per_deg_lat * math.cos(math.radians(lat0))

    def to_xy(self, lat: float, lng: float) -> tuple[float, float]:
        return (lng - self.lng0) * self._m_per_deg_lng, (lat - self.lat0) * self._m_per_deg_lat

    def to_latlng(self, x: float, y: float) -> tuple[float, float]:
        return self.lat0 + y / self._m_per_deg_lat, self.lng0 + x / self._m_per_deg_lng

    def bearing_radius_to_xy(self, bearing_deg: float, r_m: float) -> tuple[float, float]:
        b = math.radians(bearing_deg)
        return r_m * math.sin(b), r_m * math.cos(b)


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def polygon_area_m2(coords: list[tuple[float, float]]) -> float:
    """鞋带公式，输入为平面坐标 (x, y) 米。"""
    if len(coords) < 3:
        return 0.0
    s = 0.0
    for i in range(len(coords)):
        x1, y1 = coords[i]
        x2, y2 = coords[(i + 1) % len(coords)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0
