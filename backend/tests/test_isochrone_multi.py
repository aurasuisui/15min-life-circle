"""多档位一次性采样的配额与几何正确性测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.isochrone import (
    SampleConfig,
    build_isochrone,
    build_isochrones_multi,
    sample_field,
)
from app.services.providers import MockRouteProvider

CENTER = (31.2045, 121.5990)
CFG = SampleConfig(rays=36, dr=200.0, densify_per_crossing=3)


def test_multi_costs_same_as_single_max_tier():
    """三档一起做，配额必须等于只做最大档位，而不是三倍。"""
    p1 = MockRouteProvider()
    multi = build_isochrones_multi(p1, CENTER, (300.0, 600.0, 900.0), CFG)
    charged_multi = multi[900.0].routes_charged

    p2 = MockRouteProvider()
    single = build_isochrone(p2, CENTER, 900.0, CFG)   # 注意：build_isochrone 的 target 单位是【秒】
    charged_single = single.routes_charged

    assert charged_multi == charged_single, (
        f"多档位配额 {charged_multi} 应等于单档位 {charged_single}"
    )
    print(f"  [OK] 三档一起做 = {charged_multi} 条路线（单档位也是 {charged_single}）")


def test_multi_much_cheaper_than_three_separate_runs():
    """对比：三档各跑一次会烧掉约 3 倍配额。"""
    p = MockRouteProvider()
    total = sum(
        build_isochrone(p, CENTER, s, CFG).routes_charged for s in (300.0, 600.0, 900.0)
    )
    p2 = MockRouteProvider()
    multi = build_isochrones_multi(p2, CENTER, (300.0, 600.0, 900.0), CFG)[900.0].routes_charged
    assert total > multi * 2, "分别跑应当显著更贵"
    print(f"  [OK] 分别跑 {total} 条 vs 一次跑 {multi} 条（省 {total / multi:.1f} 倍）")


def test_areas_are_monotonic_in_time():
    """时间越长可达面积越大。"""
    p = MockRouteProvider()
    res = build_isochrones_multi(p, CENTER, (300.0, 600.0, 900.0), CFG)
    a5, a10, a15 = res[300.0].area_m2, res[600.0].area_m2, res[900.0].area_m2
    assert 0 < a5 < a10 < a15, f"面积应递增，实际 {a5:.0f} / {a10:.0f} / {a15:.0f}"
    print(f"  [OK] 面积单调：5min {a5/1e6:.3f} -> 10min {a10/1e6:.3f} -> 15min {a15/1e6:.3f} km^2")


def test_all_three_have_polygons():
    p = MockRouteProvider()
    res = build_isochrones_multi(p, CENTER, (300.0, 600.0, 900.0), CFG)
    for t, r in res.items():
        assert len(r.polygon_xy) >= 4, f"{t}s 档位多边形点数不足"
        assert not r.warnings, f"{t}s 档位有告警: {r.warnings}"
    print(f"  [OK] 三档均产出闭合多边形，点数 {[len(r.polygon_xy) for r in res.values()]}")
