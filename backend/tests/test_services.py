"""服务层单元测试：POI 清洗 / 覆盖统计 / 可达性 / 盲区 / 评分。**零网络。**"""
from __future__ import annotations

from tests._helpers import BACKEND, use_mode  # noqa: F401

import math

import numpy as np

from app.constants import (
    BLIND_SPOT_CATEGORIES,
    BLIND_SPOT_DISTANCE_M,
    CATEGORY_KEYS,
    GROUPS,
)
from app.providers import DEFAULT_CENTER
from app.providers.barriers import zhangjiang_model
from app.providers.mock import MockPlaceProvider, MockRouteProvider, xy_to_latlng
from app.services import poi as poi_svc
from app.services.accessibility import (
    build_access_grid,
    blind_mask,
    correction_band,
    effective_distance,
    pair_time_s,
)
from app.services.blindspot import detect_blindspots, suggest_siting
from app.services.coverage import compute_coverage
from app.services.geometry import cells_to_polygons, largest_polygon, to_geojson
from app.services.score import compute_score, grade_of


# ---------------------------------------------------------------- POI 清洗
def _raw_records():
    p = MockPlaceProvider(center=DEFAULT_CENTER)
    recs, notes, metas = poi_svc.fetch_raw(p, DEFAULT_CENTER, 1500)
    return recs, notes, metas


def test_fetch_raw_uses_two_requests():
    """2 次请求覆盖 8 类（多关键字并集按【请求】计费）。"""
    recs, notes, metas = _raw_records()
    assert len(metas) == 2, f"应当只发 2 次检索请求，实际 {len(metas)}"
    assert len(GROUPS["medical"]) == 8
    assert len(GROUPS["shopping_edu_transit_green"]) == 9
    assert len(recs) >= 25


def test_normalize_name_rules():
    n = poi_svc.normalize_name
    assert n("张江农贸市场（科苑路店）") == n("张江农贸市场（科苑路店）")
    assert "（" not in n("华氏大药房(张江店)")
    assert n("ＡＢＣ超市") == n("ABC超市")
    assert n("") == ""


def test_geohash7_is_stable_and_locally_sensitive():
    a = poi_svc.geohash7(31.2045, 121.5990)
    b = poi_svc.geohash7(31.2045, 121.5990)
    c = poi_svc.geohash7(31.2145, 121.5990)
    assert a == b and len(a) == 7 and a != c


def test_classify_poi_dual_evidence():
    key, conf, why = poi_svc.classify_poi("张江农贸市场", "购物;市场")
    assert key == "market" and conf >= 2.0
    key, conf, why = poi_svc.classify_poi("社区药房", "医疗;诊所")
    assert key == "pharmacy", f"名称证据应推翻 tag，实际 {key}"
    key, _, _ = poi_svc.classify_poi("张江建材市场", "购物;市场")
    assert key != "market", "建材市场必须被排除"
    key, _, _ = poi_svc.classify_poi("张江小学附属幼儿园", "教育培训;幼儿园")
    assert key != "primary_school"
    key, _, _ = poi_svc.classify_poi("张江生鲜超市", "购物;市场")
    assert key == "supermarket", "名称证据应胜出"


def test_cleaning_pipeline_funnel_is_monotone():
    recs, _, _ = _raw_records()
    poly = [ [(121.590, 31.200), (121.610, 31.200), (121.610, 31.212), (121.590, 31.212)] ]
    res = poi_svc.build_cleaning_result(recs, poly)
    f = res.counters
    assert f.raw == len(recs)
    assert f.deduped <= f.raw, "去重后不应变多"
    assert f.corrected <= f.deduped, "类型纠正后不应变多"
    assert f.inside <= f.corrected, "圈内裁剪后不应变多"
    assert f.removed_wrong_type >= 1, "至少应剔除 1 条类型错标记录"
    assert f.drift_corrected >= 1, "应至少校正 1 条坐标漂移"
    assert f.dropped_outside >= 1, "应至少有 1 条落在圈外"


def test_cleaning_log_is_auditable():
    recs, _, _ = _raw_records()
    res = poi_svc.build_cleaning_result(recs, None)
    joined = "\n".join(res.log)
    for token in ("L2 去重", "L3 坐标漂移校正", "L4 类型纠正", "L6 圈内裁剪", "清洗漏斗"):
        assert token in joined, f"清洗日志应包含 {token}"


def test_drift_correction_moves_outlier_to_anchor():
    a = poi_svc.PoiRecord(uid="U1", name="X药房", lat=31.2045, lng=121.5990, tag="医疗;药店")
    b = poi_svc.PoiRecord(uid="U1", name="X药房", lat=31.2065, lng=121.5990, tag="医疗;药店")
    log: list[str] = []
    n = poi_svc.correct_drift([a, b], log, outlier_m=100.0)
    assert n == 1
    assert b.corrected_xy is True
    assert abs(b.lat - a.lat) < 1e-9
    assert b.drift_m > 100.0


def test_clip_inside_uses_geojson_ring():
    poly = [[(121.5990, 31.2045), (121.6090, 31.2045), (121.6090, 31.2145), (121.5990, 31.2145)]]
    inside = poi_svc.PoiRecord(uid="A", name="a", lat=31.2095, lng=121.6040)
    outside = poi_svc.PoiRecord(uid="B", name="b", lat=31.1945, lng=121.6040)
    ins, outs = poi_svc.clip_inside([inside, outside], poly)
    assert [r.uid for r in ins] == ["A"]
    assert [r.uid for r in outs] == ["B"]


# ---------------------------------------------------------------- 几何
def test_largest_polygon_picks_biggest_part():
    from shapely.geometry import Polygon

    small = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    big = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    from shapely.geometry import MultiPolygon

    assert largest_polygon(MultiPolygon([small, big])).area == 100.0
    assert largest_polygon(None) is None


def test_to_geojson_closes_ring():
    gj = to_geojson([[(121.0, 31.0), (121.1, 31.0), (121.1, 31.1)]])
    ring = gj["coordinates"][0]
    assert ring[0] == ring[-1], "多边形必须闭合（CONTRACT §4.1）"
    assert gj["type"] == "Polygon"
    # 坐标顺序必须是 [lng, lat]
    assert abs(ring[0][0] - 121.0) < 1e-9


def test_cells_to_polygons_merges_connected_cells():
    xs = np.array([0.0, 125.0, 250.0, 375.0])
    ys = np.array([0.0, 125.0])
    mask = np.zeros((2, 4), dtype=bool)
    mask[0, 0] = mask[0, 1] = True   # 相邻两格 -> 合成一个多边形
    polys = cells_to_polygons(xs, ys, mask, 125.0, min_area_m2=5000.0)
    assert len(polys) == 1
    assert 5000.0 < polys[0].area < 2 * 125.0 * 125.0 * 1.2
    # 形态学闭运算后仍是矩形，不应退化成 3~4 个点的退化环
    assert len(polys[0].exterior.coords) >= 5


def test_cells_to_polygons_drops_tiny_fragments():
    xs = np.array([0.0, 125.0, 250.0])
    ys = np.array([0.0, 125.0])
    mask = np.zeros((2, 3), dtype=bool)
    mask[0, 0] = mask[0, 1] = True
    mask[1, 2] = True                # 孤立一格
    polys = cells_to_polygons(xs, ys, mask, 125.0, min_area_m2=16000.0)
    assert len(polys) == 1, "最小面积阈值应滤掉孤立小碎块"
    assert polys[0].area >= 16000.0


# ---------------------------------------------------------------- 可达性 / 盲区
def _grid(facilities):
    from app.core.isochrone import SampleConfig, contour_to_polygon, sample_field

    cfg = SampleConfig(rays=36, dr=200.0, densify_per_crossing=3)
    bundle = sample_field(MockRouteProvider(center=DEFAULT_CENTER), DEFAULT_CENTER, 900.0, cfg)
    geom = contour_to_polygon(bundle.xs, bundle.ys, bundle.z, bundle.mask, 900.0)
    return build_access_grid(zhangjiang_model(), DEFAULT_CENTER, geom, facilities, 125.0)


def test_access_grid_marks_river_impassable():
    grid = _grid({"market": np.array([[0.0, 0.0]])})
    band = ~grid.passable
    assert band.any(), "河道带必须被标记为不可通行"
    y_band = grid.gy[band]
    assert float(y_band.min()) >= -295.1 and float(y_band.max()) <= -204.9


def test_pair_time_charges_barrier():
    m = zhangjiang_model()
    near = pair_time_s(m, 0.0, -300.0, np.array([0.0]), np.array([-350.0]))[0]
    far = pair_time_s(m, 0.0, -300.0, np.array([0.0]), np.array([-800.0]))[0]
    assert far > near, "同侧更远的设施耗时更长"


def test_correction_band_brackets_threshold():
    grid = _grid({"market": np.array([[0.0, 0.0], [600.0, 500.0]], dtype=float)})
    band = correction_band(grid, "market", BLIND_SPOT_DISTANCE_M)
    d = grid.dist_min["market"]
    assert band.sum() > 0, "应当存在落在阈值附近的不确定带"
    assert d[band].min() >= 0.70 * BLIND_SPOT_DISTANCE_M - 1e-6
    assert d[band].max() <= 1.60 * BLIND_SPOT_DISTANCE_M + 1e-6


def test_blind_mask_uses_distance_semantics():
    """盲区 = 步行路网距离 > 1000 m（任务书 FR-4 原文口径）。"""
    near = np.array([[0.0, 0.0]], dtype=float)
    grid = _grid({"market": near})
    mask = blind_mask(grid, "market", BLIND_SPOT_DISTANCE_M, None, zhangjiang_model())
    eff = effective_distance(grid, "market", None, zhangjiang_model())
    assert mask.any(), "应当存在盲区格网"
    assert eff[mask].min() > BLIND_SPOT_DISTANCE_M - 1e-6


def test_detect_blindspots_reports_missing_categories():
    facilities = {
        "market": np.array([[-500.0, -300.0], [600.0, 400.0]], dtype=float),
        "pharmacy": np.array([[240.0, 380.0], [-400.0, 500.0]], dtype=float),
        "primary_school": np.array([[150.0, -480.0]], dtype=float),
    }
    grid = _grid(facilities)
    res = detect_blindspots(grid, {}, BLIND_SPOT_DISTANCE_M, model=zhangjiang_model())
    assert isinstance(res.total_area_m2, float)
    if res.polygons:
        for p in res.polygons:
            assert p.area_m2 > 0
            for k in p.missing:
                assert k in BLIND_SPOT_CATEGORIES
    # 只有一所小学 -> 一定存在小学盲区
    assert res.polygons, "应当至少识别出一处盲区"


def test_suggest_siting_returns_contract_shape():
    facilities = {
        "market": np.array([[-500.0, -300.0]], dtype=float),
        "pharmacy": np.array([[240.0, 380.0]], dtype=float),
        "primary_school": np.array([[150.0, -480.0]], dtype=float),
    }
    grid = _grid(facilities)
    res = detect_blindspots(grid, {}, BLIND_SPOT_DISTANCE_M, model=zhangjiang_model())
    from app.core.geo import LocalProjection

    sites = suggest_siting(res, LocalProjection(*DEFAULT_CENTER))
    assert sites, "存在盲区时应给出补点建议"
    for s in sites:
        for key in ("lat", "lng", "reason", "gains"):
            assert key in s, f"补点建议缺少字段 {key}"
        assert isinstance(s["gains"], dict)


# ---------------------------------------------------------------- 覆盖 + 评分
def test_coverage_metrics_and_scores():
    recs, _, _ = _raw_records()
    res = poi_svc.build_cleaning_result(recs, None)
    cov = compute_coverage(res.records, 2_000_000.0, DEFAULT_CENTER, route_provider=None)
    assert len(cov.stats) == 8
    for s in cov.stats:
        assert s.key in CATEGORY_KEYS
        assert s.count >= 0
        assert 0.0 <= s.coverage_score <= 1.0
        assert s.density_per_km2 >= 0.0
        if s.pois:
            assert all("walk_seconds" in p for p in s.pois)


def test_coverage_with_mock_routes_uses_real_times():
    recs, _, _ = _raw_records()
    res = poi_svc.build_cleaning_result(recs, None)
    prov = MockRouteProvider(center=DEFAULT_CENTER)
    cov = compute_coverage(res.records, 2_000_000.0, DEFAULT_CENTER, route_provider=prov)
    assert cov.routes_charged > 0
    assert cov.by_key()["market"].nearest_walk_seconds > 0


def test_score_dimensions_and_grade():
    recs, _, _ = _raw_records()
    res = poi_svc.build_cleaning_result(recs, None)
    cov = compute_coverage(res.records, 2_000_000.0, DEFAULT_CENTER)
    sc = compute_score(cov.stats)
    assert 0.0 <= sc.total <= 100.0
    assert sc.grade in ("A", "B", "C", "D")
    assert len(sc.dimensions) == 8, f"8 维评分，实际 {len(sc.dimensions)}"
    wsum = sum(d.weight for d in sc.dimensions)
    assert abs(wsum - 1.0) < 1e-6, f"权重之和应为 1，实际 {wsum}"
    for d in sc.dimensions:
        assert 0.0 <= d.score <= 100.0
        assert isinstance(d.raw_count, int)


def test_grade_bands():
    assert grade_of(90) == "A"
    assert grade_of(75) == "B"
    assert grade_of(60) == "C"
    assert grade_of(10) == "D"
