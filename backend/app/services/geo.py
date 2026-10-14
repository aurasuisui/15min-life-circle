"""地理计算工具。

坐标约定：内部统一使用 BD09（百度经纬度），因为百度地图所有接口默认 bd09ll。
距离较小时（< 5km）使用等距圆柱近似，误差可忽略且远快于 haversine。
"""
from __future__ import annotations

import math

EARTH_RADIUS_M = 6371008.8


def destination_point(lat: float, lng: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    """从 (lat, lng) 沿 bearing_deg 方向前进 distance_m，返回 (lat, lng)。

    等距圆柱近似：在 < 5km 尺度上误差 < 0.1%。
    """
    br = math.radians(bearing_deg)
    dlat = (distance_m * math.cos(br)) / EARTH_RADIUS_M
    dlng = (distance_m * math.sin(br)) / (EARTH_RADIUS_M * math.cos(math.radians(lat)))
    return lat + math.degrees(dlat), lng + math.degrees(dlng)


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两点间大圆距离（米）。a/b 为 (lat, lng)。"""
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def polygon_area_m2(points: list[tuple[float, float]], lat_ref: float) -> float:
    """闭合多边形面积（平方米），输入为 (lat, lng) 列表。"""
    if len(points) < 3:
        return 0.0
    m_per_deg_lat = math.radians(1) * EARTH_RADIUS_M
    m_per_deg_lng = m_per_deg_lat * math.cos(math.radians(lat_ref))
    xy = [(p[1] * m_per_deg_lng, p[0] * m_per_deg_lat) for p in points]
    s = 0.0
    for i in range(len(xy)):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % len(xy)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0
