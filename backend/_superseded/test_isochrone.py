"""等时圈核心算法测试。

MockRouteProvider 的耗时对半径是【仿射】的（t = 1.25*r/speed + overhead），
因此线性插值应当能【精确】反解出 t = 900s 对应的半径，可与解析解逐方位比对。
"""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.geo import destination_point, haversine_m, polygon_area_m2
from app.services.isochrone import (
    MAX_ROUTES_PER_REQUEST,
    build_isochrone,
    chaikin_smooth,
    drop_outliers,
    plan_samples,
    solve_boundary,
)
from app.services.providers import (
    MockRouteProvider,
    QuotaLedger,
    QuotaExhaustedError,
)

CENTER = (31.2304, 121.4737)  # 上海人民广场附近
RADII = (500.0, 1000.0, 1500.0)
DIRECTIONS = 32


# --------------------------------------------------------------------------
# 几何工具
# --------------------------------------------------------------------------
def test_destination_point_matches_haversine():
    """等距圆柱近似与 haversine 在小尺度上应一致（< 0.1%）。"""
    for bearing in range(0, 360, 15):
        for r in (200.0, 800.0, 2000.0):
            p = destination_point(CENTER[0], CENTER[1], bearing, r)
            assert haversine_m(CENTER, p) == pytest.approx(r, rel=1e-3)


def test_polygon_area_of_regular_ring():
    """半径 1000m 的 64 边形面积应接近 pi*r^2。"""
    pts = [
        destination_point(CENTER[0], CENTER[1], 360.0 * i / 64, 1000.0)
        for i in range(64)
    ]
    area = polygon_area_m2(pts, CENTER[0])
    assert area == pytest.approx(math.pi * 1000.0 ** 2, rel=0.01)


# --------------------------------------------------------------------------
# 采样与配额
# --------------------------------------------------------------------------
def test_plan_samples_count():
    samples = plan_samples(CENTER, DIRECTIONS, RADII)
    assert len(samples) == DIRECTIONS * len(RADII) == 96
    assert len({s.direction_idx for s in samples}) == DIRECTIONS


def test_walking_routes_split_into_two_requests():
    """96 个采样点 > 单次上限 50，必须拆成 2 次请求。"""
    provider = MockRouteProvider()
    result = build_isochrone(provider, CENTER, 15.0, DIRECTIONS, RADII)
    assert len(provider.calls) == 2
    assert all(c <= MAX_ROUTES_PER_REQUEST for c in provider.calls)
    assert sum(provider.calls) == 96
    assert result.routes_charged == 96


# --------------------------------------------------------------------------
# 插值精度：与解析解比对
# --------------------------------------------------------------------------
def test_interpolated_radius_matches_analytic_solution():
    """逐方位验证反解半径 == 解析解 (900-overhead)*speed/1.25。"""
    provider = MockRouteProvider()
    target_s = 15 * 60.0
    samples = plan_samples(CENTER, DIRECTIONS, RADII)
    from app.services.isochrone import fetch_durations

    fetch_durations(provider, CENTER, samples)
    _, radii_out, _ = solve_boundary(CENTER, samples, target_s, DIRECTIONS)

    worst = 0.0
    for bearing, r_actual in radii_out:
        speed = provider.speed_at(bearing)
        r_expected = (target_s - provider.overhead_s) * speed / provider.detour_factor
        rel = abs(r_actual - r_expected) / r_expected
        worst = max(worst, rel)

    assert worst < 1e-3, f"最大相对误差 {worst:.2e} 超出容差"


def test_isochrone_is_anisotropic():
    """等时圈应体现路网方向性：优先方位(45度)的半径明显大于反方向(225度)。"""
    provider = MockRouteProvider()
    result = build_isochrone(provider, CENTER, 15.0, DIRECTIONS, RADII)
    by_bearing = dict(result.boundary_by_direction)
    assert by_bearing[45.0] > by_bearing[225.0] * 1.5


# --------------------------------------------------------------------------
# 边界与降级
# --------------------------------------------------------------------------
def test_extrapolation_is_clamped():
    """目标时长远超采样范围时，外推必须有上限，不能无限外扩。"""
    provider = MockRouteProvider()
    result = build_isochrone(provider, CENTER, 90.0, DIRECTIONS, RADII)
    max_r = max(r for _, r in result.boundary_by_direction)
    assert max_r <= 2.5 * 1500.0
    assert any("外推" in w for w in result.warnings)


def test_insufficient_samples_fall_back():
    """某方位无有效数据时使用兜底速度，并记录 warning。"""
    samples = plan_samples(CENTER, 4, RADII)
    for s in samples:
        s.duration_s = None
    boundary, _, warnings = solve_boundary(CENTER, samples, 900.0, 4)
    assert len(boundary) == 4
    assert len(warnings) == 4


def test_missing_individual_points_are_tolerated():
    """部分采样点返回 None（API 无结果）不应导致整体失败。"""
    provider = MockRouteProvider()
    samples = plan_samples(CENTER, DIRECTIONS, RADII)
    from app.services.isochrone import fetch_durations

    fetch_durations(provider, CENTER, samples)
    for i, s in enumerate(samples):
        if i % 7 == 0:
            s.duration_s = None
    boundary, _, _ = solve_boundary(CENTER, samples, 900.0, DIRECTIONS)
    assert len(boundary) == DIRECTIONS


def test_area_is_reasonable():
    """15 分钟步行圈的等效半径应在 0.6~1.2 km。

    量纲校验：步行 1.2m/s、绕行系数 1.25 -> 有效速度约 0.96m/s，
    900s 对应直线距离约 0.86km。面积量级应在 1.5~4.5 km^2。
    """
    provider = MockRouteProvider()
    result = build_isochrone(provider, CENTER, 15.0, DIRECTIONS, RADII)

    radii = [r for _, r in result.boundary_by_direction]
    mean_r = sum(radii) / len(radii)
    assert 600.0 < mean_r < 1200.0, f"等效半径 {mean_r:.0f}m 不合理"
    assert 1.5e6 < result.area_m2 < 4.5e6, f"面积 {result.area_m2/1e6:.2f} km^2 不合理"


# --------------------------------------------------------------------------
# 平滑与离群点
# --------------------------------------------------------------------------
def test_chaikin_doubles_points_and_preserves_shape():
    pts = plan_samples(CENTER, 8, (1000.0,))
    ring = [(p.lat, p.lng) for p in pts]
    out = chaikin_smooth(ring, 2)
    assert len(out) == 8 * 4
    assert polygon_area_m2(out, CENTER[0]) == pytest.approx(
        polygon_area_m2(ring, CENTER[0]), rel=0.12
    )


def test_drop_outliers_removes_spike():
    ring = [
        destination_point(CENTER[0], CENTER[1], 360.0 * i / 16, 1000.0)
        for i in range(16)
    ]
    ring[3] = destination_point(CENTER[0], CENTER[1], 67.5, 20000.0)  # 异常尖刺
    cleaned = drop_outliers(ring, CENTER)
    assert len(cleaned) == 15


# --------------------------------------------------------------------------
# 配额账本：按【路线数】计费，不是按请求数
# --------------------------------------------------------------------------
def test_quota_charges_by_route_count_not_requests():
    ledger = QuotaLedger(daily_route_limit=5000, daily_poi_limit=100)
    ledger.charge_routes(48)
    ledger.charge_routes(48)
    assert ledger.routes_used == 96
    assert ledger.requests_made == 2


def test_quota_raises_when_exhausted():
    ledger = QuotaLedger(daily_route_limit=50, daily_poi_limit=100)
    ledger.charge_routes(48)
    with pytest.raises(QuotaExhaustedError):
        ledger.charge_routes(48)


def test_poi_quota_is_the_bottleneck():
    """验证设计假设：算路配额充裕，POI 检索才是瓶颈。

    32 方位 x 3 半径 = 96 条路线/等时圈 -> 约 52 圈/日
    8 类 x 2 页          = 16 次/社区    -> 约  6 社区/日
    """
    ledger = QuotaLedger(daily_route_limit=5000, daily_poi_limit=100)
    isochrones_per_day = ledger.daily_route_limit // (DIRECTIONS * len(RADII))
    poi_categories, pages = 8, 2
    communities_per_day = ledger.daily_poi_limit // (poi_categories * pages)

    assert isochrones_per_day == 52
    assert communities_per_day == 6
    assert isochrones_per_day > communities_per_day * 5, "算路配额应显著宽裕于 POI"
