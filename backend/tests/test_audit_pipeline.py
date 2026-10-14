"""体检流水线端到端测试（mock / replay，**零网络、零配额**）+ 故障注入。

故障注入用 `respx` 拦截 httpx，覆盖 SOLUTION §5.2 表里的四类：
  * 配额耗尽（status=302）-> 快速失败，不重试
  * QPS 限流（status=401）-> 退避重试
  * 瞬时故障（HTTP 500）-> 退避重试
  * SN 校验失败（status=211）-> 熔断不重试
"""
from __future__ import annotations

from tests._helpers import BACKEND, SEED_DIR, use_mode  # noqa: F401

import json
import time

from app.constants import (
    BLIND_SPOT_CATEGORIES,
    BLIND_SPOT_DISTANCE_M,
    CATEGORY_KEYS,
    DEFAULT_MINUTES,
)
from app.providers import DEFAULT_CENTER
from app.schemas.report import AuditReport
from app.services.audit import AuditOptions, _template_ai_report, build_ai_report, run_audit


def _audit(mode: str = "mock", **kw):
    use_mode(mode, "zhangjiang-haike" if mode == "replay" else None)
    opts = AuditOptions(**kw)
    return run_audit(opts)


# ---------------------------------------------------------------- 结构
def test_audit_report_matches_contract():
    """返回的 JSON 必须能通过 Pydantic 严格校验（字段名对齐 CONTRACT §4）。"""
    out = _audit("mock")
    body = {k: v for k, v in out.report.items() if not k.startswith("_")}
    AuditReport.model_validate(body)


def test_meta_section():
    out = _audit("mock")
    meta = out.report["meta"]
    assert set(meta) >= {"center", "address", "generated_at", "data_mode", "degraded",
                         "degrade_reasons", "quota"}
    assert meta["data_mode"] == "mock"
    assert isinstance(meta["degrade_reasons"], list)
    q = meta["quota"]
    assert set(q) == {"routes_used", "routes_limit", "poi_used", "poi_limit"}


def test_isochrones_three_levels_and_geometry():
    out = _audit("mock")
    iso = out.report["isochrones"]
    assert set(iso) == {str(m) for m in DEFAULT_MINUTES}
    areas = [iso[str(m)]["area_m2"] for m in DEFAULT_MINUTES]
    assert areas[0] < areas[1] < areas[2], f"等时圈面积必须随时长单调递增：{areas}"
    for m in DEFAULT_MINUTES:
        item = iso[str(m)]
        assert item["target_seconds"] == m * 60
        assert item["n_samples"] > 100
        geom = item["geometry"]
        assert geom["type"] == "Polygon"
        ring = geom["coordinates"][0]
        assert len(ring) >= 4
        assert ring[0] == ring[-1], "多边形必须闭合"
        for lng, lat in ring[:5]:
            assert 121.4 < lng < 121.8 and 31.1 < lat < 31.3, "坐标必须是 BD09 经纬度"


def test_isochrone_uses_single_shared_sampling():
    """三档共用一次采样：routes_charged 相同且远小于三档分别采样的总和。"""
    out = _audit("mock")
    iso = out.report["isochrones"]
    charged = {m: iso[str(m)]["routes_charged"] for m in DEFAULT_MINUTES}
    assert len(set(charged.values())) == 1, f"三档应共享同一次采样，实际 {charged}"
    assert charged[15] < 600, f"一次采样应控制在 600 条路线内，实际 {charged[15]}"


def test_facilities_eight_categories():
    out = _audit("mock")
    cats = out.report["facilities"]["categories"]
    assert [c["key"] for c in cats] == list(CATEGORY_KEYS)
    for c in cats:
        assert set(c) >= {"key", "name", "query", "count", "density_per_km2",
                          "nearest_walk_seconds", "coverage_score", "pois"}
        assert 0.0 <= c["coverage_score"] <= 1.0
        assert "$" in c["query"] or c["key"] in ("primary_school", "bus_stop")


def test_cleaning_funnel_is_monotone_and_meaningful():
    out = _audit("mock")
    f = out.report["facilities"]["cleaning_funnel"]
    assert set(f) == {"raw", "deduped", "corrected", "inside"}
    assert f["raw"] > f["deduped"] >= f["corrected"] >= f["inside"] > 0
    assert f["raw"] >= 20, "mock 场景应召回足够多的原始 POI"


def test_blindspots_structure():
    out = _audit("mock")
    bs = out.report["blindspots"]
    assert "total_area_m2" in bs and "polygons" in bs
    for p in bs["polygons"]:
        assert set(p) == {"area_m2", "missing", "sample_point", "geometry"}
        assert p["area_m2"] > 0
        assert isinstance(p["missing"], list)
        for k in p["missing"]:
            assert k in BLIND_SPOT_CATEGORIES
        assert set(p["sample_point"]) == {"lat", "lng"}
        ring = p["geometry"]["coordinates"][0]
        assert ring[0] == ring[-1]
    assert bs["total_area_m2"] > 0, "mock 场景刻意留有小学盲区"


def test_score_eight_dimensions():
    out = _audit("mock")
    sc = out.report["score"]
    assert 0.0 <= sc["total"] <= 100.0
    assert sc["grade"] in ("A", "B", "C", "D")
    assert len(sc["dimensions"]) == 8
    for d in sc["dimensions"]:
        assert set(d) == {"key", "name", "score", "weight", "raw_count"}


def test_siting_contract_shape():
    out = _audit("mock")
    for s in out.report["siting"]:
        assert set(s) == {"lat", "lng", "reason", "gains"}
        assert isinstance(s["gains"], dict)


def test_ai_report_never_breaks_api():
    out = _audit("mock")
    ai = out.report["ai_report"]
    for key in ("degraded", "diagnosis", "evidence", "suggestions"):
        assert key in ai, f"ai_report 缺少 {key}"
    assert isinstance(ai["diagnosis"], str) and ai["diagnosis"]
    for s in ai["suggestions"]:
        assert s["priority"] in ("high", "medium", "low")


def test_ai_layer_missing_falls_back_to_template():
    """S4 的 app.ai 不存在时，必须产出模板结果，绝不能抛异常。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "app.ai" or name.startswith("app.ai."):
            raise ImportError("simulated: app/ai not implemented yet")
        return real_import(name, *a, **k)

    builtins.__import__ = fake_import
    try:
        out = _audit("mock")
        ai = out.report["ai_report"]
        assert ai["degraded"] is True
        assert ai["diagnosis"]
        assert ai["evidence"]
        assert out.report["_debug"]["ai_fallback_reason"].startswith("app.ai 不可用")
    finally:
        builtins.__import__ = real_import


def test_template_ai_report_shape():
    rep = {"facilities": {"categories": [{"key": "market", "name": "菜市场", "count": 0,
                                          "coverage_score": 0.0}]},
           "blindspots": {"polygons": []}, "score": {"total": 10, "grade": "D"}}
    tpl = _template_ai_report(rep, "test")
    assert tpl["degraded"] is True
    assert "完全缺失" in tpl["diagnosis"]
    assert tpl["suggestions"]


# ---------------------------------------------------------------- replay 一致性
def test_replay_audit_matches_recorded_expectations():
    use_mode("replay", "zhangjiang-haike")
    out = run_audit(AuditOptions(lat=DEFAULT_CENTER[0], lng=DEFAULT_CENTER[1]))
    rep = out.report
    idx = json.loads((SEED_DIR / "zhangjiang-haike" / "index.json").read_text(encoding="utf-8"))
    exp = idx["expected"]
    for k, want in exp["isochrone_area_km2"].items():
        got = round(rep["isochrones"][k]["area_m2"] / 1e6, 4)
        assert abs(got - want) / max(want, 1e-9) < 0.01, f"{k}min 面积 {got} != 快照期望 {want}"
    assert rep["facilities"]["cleaning_funnel"] == exp["cleaning_funnel"]
    assert len(rep["blindspots"]["polygons"]) == exp["blindspot_count"]
    assert rep["score"]["total"] == exp["score_total"]


def test_replay_mode_declares_zero_quota_and_snapshot():
    out = _audit("replay")
    reasons = " ".join(out.report["meta"]["degrade_reasons"])
    assert "零配额" in reasons
    assert out.report["meta"]["quota"]["routes_used"] == 0
    assert out.report["meta"]["quota"]["poi_used"] == 0


def test_replay_is_deterministic():
    a = _audit("replay")
    b = _audit("replay")
    for k in DEFAULT_MINUTES:
        assert a.report["isochrones"][str(k)]["area_m2"] == b.report["isochrones"][str(k)]["area_m2"]
    assert a.report["score"]["total"] == b.report["score"]["total"]


# ---------------------------------------------------------------- 性能
def test_mock_audit_is_fast():
    use_mode("mock")
    t0 = time.perf_counter()
    run_audit(AuditOptions())
    dt = time.perf_counter() - t0
    assert dt < 60.0, f"mock 全链路应远快于 60s，实际 {dt:.1f}s"


# ---------------------------------------------------------------- 故障注入（respx，零网络）
def _resp(status: str, body: dict, code: int = 200):
    import httpx

    return httpx.Response(code, json=body, headers={"content-type": "application/json"})


def test_inject_quota_exhausted_fails_fast():
    import respx

    from app.baidu.cache import FileCache
    from app.baidu.client import BaiduClient
    from app.baidu.errors import QuotaExhaustedError
    from app.baidu.quota import QuotaLedger

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith="https://api.map.baidu.com/place/v3/around").mock(
            return_value=_resp("302", {"status": 302, "message": "天配额超限"}))
        led = QuotaLedger(db_path=None, limits={"place": 100})
        cli = BaiduClient(ak="fake", sk="", ledger=led, cache=FileCache("__none__", enabled=False))
        try:
            cli.place_around("药店", (31.2, 121.6))
            raise AssertionError("配额耗尽必须快速失败")
        except QuotaExhaustedError:
            pass
        assert route.call_count == 1, "配额耗尽绝不能重试"
        assert led.snapshot()["services"]["place"]["level"] == "exhausted", "账本应被置满"


def test_inject_qps_limit_retries_then_succeeds():
    import respx

    from app.baidu.cache import FileCache
    from app.baidu.client import BaiduClient
    from app.baidu.quota import QuotaLedger

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith="https://api.map.baidu.com/place/v3/around").mock(
            side_effect=[_resp("401", {"status": 401, "message": "并发超限"}),
                         _resp("0", {"status": 0, "results": [{"uid": "u", "name": "N",
                                                               "location": {"lat": 31.2, "lng": 121.6}}]})])
        cli = BaiduClient(ak="fake", sk="", ledger=QuotaLedger(db_path=None),
                          cache=FileCache("__none__", enabled=False), qps=1000.0)
        cli._get_with_retries = cli._get_with_retries  # noqa: B018  (保持可读性)
        out = cli.place_around("药店", (31.2, 121.6))
        assert out["status"] == 0
        assert route.call_count == 2, "401 应退避重试一次后成功"


def test_inject_http_500_retries_then_gives_up():
    import respx

    from app.baidu.cache import FileCache
    from app.baidu.client import BaiduClient
    from app.baidu.errors import TransientApiError
    from app.baidu.quota import QuotaLedger

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith="https://api.map.baidu.com/place/v3/around").mock(
            return_value=_resp("-1", {"error": "boom"}, code=500))
        led = QuotaLedger(db_path=None, limits={"place": 100})
        cli = BaiduClient(ak="fake", sk="", ledger=led, cache=FileCache("__none__", enabled=False),
                          qps=1000.0, max_retries=2)
        try:
            cli.place_around("药店", (31.2, 121.6))
            raise AssertionError("持续 500 最终必须抛错")
        except TransientApiError:
            pass
        assert route.call_count == 3, f"max_retries=2 应共尝试 3 次，实际 {route.call_count}"
        assert led.used("place") == 0, "失败的调用必须归还配额"


def test_inject_sn_failure_circuit_breaks():
    import respx

    from app.baidu.cache import FileCache
    from app.baidu.client import BaiduClient, endpoint_available, reset_endpoints
    from app.baidu.errors import SnVerifyError
    from app.baidu.quota import QuotaLedger

    reset_endpoints()
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith="https://api.map.baidu.com/routematrix/v2/walking").mock(
            return_value=_resp("211", {"status": 211, "message": "APP SN校验失败"}))
        cli = BaiduClient(ak="fake", sk="s", ledger=QuotaLedger(db_path=None),
                          cache=FileCache("__none__", enabled=False), qps=1000.0)
        try:
            cli.walking_matrix((31.2, 121.6), [(31.21, 121.61)])
            raise AssertionError("211 必须抛错")
        except SnVerifyError:
            pass
        assert route.call_count == 1, "211 不应重试（重试也是白烧配额）"
        assert endpoint_available("routematrix") is False, "211 后该端点应被熔断"
    reset_endpoints()


def test_place_out_of_bounds_self_check_marks_degraded():
    """POI 大量超出请求半径 -> 判定可能退化为大范围检索，写入 degrade_reasons。"""

    class _BadPlace:
        name = "bad"
        last_meta = {"source": "bad", "degraded": False, "reasons": []}

        def search(self, query, center, radius=2000, page_num=0):
            results = [{"uid": f"u{i}", "name": f"远点{i}", "tag": "医疗;药店",
                        "location": {"lat": center[0] + 1.0, "lng": center[1] + 1.0}}  # ~140 km 外
                       for i in range(10)]
            return {"status": 0, "results": results}

    use_mode("mock")
    from app.providers import registry

    registry.set_override(place=_BadPlace())
    try:
        out = run_audit(AuditOptions())
        joined = " ".join(out.report["meta"]["degrade_reasons"])
        assert "退化为大范围检索" in joined, joined
    finally:
        registry.clear_override()
