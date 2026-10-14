"""Provider 三态（mock / replay / hybrid）与双路线 provider 测试。**零网络。**"""
from __future__ import annotations

from tests._helpers import BACKEND, SEED_DIR, use_mode  # noqa: F401

from app.providers import (
    DEFAULT_CENTER,
    MockPlaceProvider,
    MockRouteProvider,
    ReplayBundle,
    ReplayPlaceProvider,
    ReplayRouteProvider,
    describe,
    find_community_dir,
    get_place_provider,
    get_route_provider,
    list_communities,
    resolve_mode,
    route_key,
)
from app.providers.base import RecordingRouteProvider, SeedMissingError, meta_of
from app.providers.barriers import AnalyticModel, BarrierLine, zhangjiang_model
from app.providers.mock import latlng_to_xy, xy_to_latlng


# ---------------------------------------------------------------- mock
def test_mock_route_is_deterministic():
    p = MockRouteProvider(center=DEFAULT_CENTER)
    dests = [xy_to_latlng(DEFAULT_CENTER, 300.0, 200.0), xy_to_latlng(DEFAULT_CENTER, -400.0, 500.0)]
    a = p.walking_matrix(DEFAULT_CENTER, dests)
    b = p.walking_matrix(DEFAULT_CENTER, dests)
    assert a == b, "mock 必须确定性"
    assert all(v is not None and v > 0 for v in a)


def test_mock_route_monotone_in_distance():
    p = MockRouteProvider(center=DEFAULT_CENTER)
    prev = 0.0
    for r in (100, 300, 600, 900, 1200):
        d = xy_to_latlng(DEFAULT_CENTER, 0.0, r)
        t = p.walking_matrix(DEFAULT_CENTER, [d])[0]
        assert t > prev, f"r={r} 耗时未随距离增加"
        prev = t


def test_mock_route_charges_river_crossing():
    """跨河必须显著贵于同侧 —— 这是盲区的物理来源。"""
    p = MockRouteProvider(center=DEFAULT_CENTER)
    north_pt = xy_to_latlng(DEFAULT_CENTER, 0.0, 400.0)
    south_pt = xy_to_latlng(DEFAULT_CENTER, 0.0, -400.0)
    tn = p.walking_matrix(DEFAULT_CENTER, [north_pt])[0]
    ts = p.walking_matrix(DEFAULT_CENTER, [south_pt])[0]
    assert ts > tn * 1.5, f"跨河耗时 {ts:.0f}s 应远大于同侧 {tn:.0f}s"
    model = zhangjiang_model()
    assert model.route_length_m((0.0, 0.0), (0.0, -400.0)) > 400.0


def test_mock_place_recall_and_dirty_data():
    p = MockPlaceProvider(center=DEFAULT_CENTER)
    medical = p.search("药店$药房$社区卫生服务中心$诊所$综合医院$养老院$敬老院$日间照料中心", DEFAULT_CENTER)
    shopping = p.search("农贸市场$菜市场$生鲜市场$超市$便利店$小学$公交车站$公园$广场", DEFAULT_CENTER)
    assert len(medical["results"]) >= 8
    assert len(shopping["results"]) >= 15
    names = [r["name"] for r in shopping["results"]]
    assert any("幼儿园" in n for n in names), "应包含被 L4 剔除的幼儿园脏数据"
    dup = [r for r in shopping["results"] if r["uid"] == "MK12"]
    assert len(dup) == 2, "MK12 应有两次上报（用于验证 L3 坐标漂移校正）"


def test_mock_place_has_uids_and_locations():
    p = MockPlaceProvider(center=DEFAULT_CENTER)
    d = p.search("小学", DEFAULT_CENTER)
    for item in d["results"]:
        assert item["uid"] and item["location"]["lat"] and item["location"]["lng"]
    assert d["status"] == 0


# ---------------------------------------------------------------- replay
def test_replay_bundle_loads():
    d = find_community_dir(SEED_DIR, "zhangjiang-haike")
    b = ReplayBundle.load(d)
    assert b.name == "zhangjiang-haike"
    assert b.index.get("center")
    assert len(b.poi) >= 2
    assert len(b.routes) >= 1


def test_replay_route_hit_and_miss():
    use_mode("replay", "zhangjiang-haike")
    prov = get_route_provider()
    b = ReplayBundle.load(find_community_dir(SEED_DIR, "zhangjiang-haike"))
    rec = b.routes[next(iter(b.routes))]
    origin = tuple(rec["origin"])
    dests = [tuple(x) for x in rec["destinations"]]
    got = prov.walking_matrix(origin, dests)
    assert len(got) == len(dests)
    assert got == rec["durations"]
    try:
        prov.walking_matrix((0.0, 0.0), [(1.0, 1.0)])
        raise AssertionError("快照缺失应当抛 SeedMissingError")
    except SeedMissingError:
        pass


def test_replay_route_key_is_stable():
    a = route_key((31.2045, 121.599), [(31.2, 121.6)])
    b = route_key((31.2045000001, 121.5990000001), [(31.2, 121.6)])
    assert a == b, "坐标 1e-9 级抖动不应改变快照键"


def test_replay_place_hit_and_miss():
    use_mode("replay", "zhangjiang-haike")
    prov = get_place_provider()
    d = prov.search("药店$药房$社区卫生服务中心$诊所$综合医院$养老院$敬老院$日间照料中心", DEFAULT_CENTER)
    assert d["status"] == 0 and len(d["results"]) >= 8
    try:
        prov.search("不存在的关键字XYZ", DEFAULT_CENTER)
        raise AssertionError("缺失 query 应抛 SeedMissingError")
    except SeedMissingError:
        pass


def test_replay_meta_declares_zero_quota():
    use_mode("replay", "zhangjiang-haike")
    m = meta_of(get_route_provider())
    assert m["source"] == "replay"
    assert any("零配额" in r for r in m["reasons"])


def test_hybrid_falls_back_to_analytic_and_says_so():
    """hybrid 模式下快照缺失 -> 解析模型兜底，但**必须**写进 reasons（绝不静默）。"""
    use_mode("hybrid", "zhangjiang-haike")
    from app.providers.registry import get_route_provider as gr

    prov = gr()
    # 一个必然不在快照里的请求
    far = [(35.0, 130.0)]
    out = prov.walking_matrix(DEFAULT_CENTER, far)
    assert len(out) == 1 and out[0] > 0
    assert prov.last_meta["reasons"], "解析兜底必须留下降级原因"


# ---------------------------------------------------------------- 双路线 provider
def test_fallback_matrix_degrades_to_directionlite():
    """routematrix 返回 211（服务未开通）时必须自动熔断并降级到 directionlite。"""
    from app.baidu.client import EndpointDegraded, reset_endpoints
    from app.baidu.errors import SnVerifyError
    from app.providers.live import DirectionLiteProvider, FallbackMatrixRouteProvider

    reset_endpoints()

    class _Matrix:
        name = "fake"

        def walking_matrix(self, origin, destinations):
            raise SnVerifyError("routematrix", 211, "APP SN校验失败")

    class _Lite:
        name = "fake-lite"
        calls = 0

        def walking_matrix(self, origin, destinations):
            _Lite.calls += 1
            return [100.0] * len(destinations)

    prov = FallbackMatrixRouteProvider(matrix=_Matrix(), lite=_Lite())
    out = prov.walking_matrix(DEFAULT_CENTER, [(31.21, 121.60), (31.22, 121.61)])
    assert out == [100.0, 100.0]
    assert _Lite.calls == 1
    assert prov.last_meta["degraded"] is True
    assert any("directionlite" in r for r in prov.last_meta["reasons"])
    assert prov.last_meta["source"] == "live:directionlite"


def test_fallback_matrix_does_not_swallow_param_errors():
    """参数错误不应当触发降级（降级只针对 211/240 这类权限/服务问题）。"""
    from app.baidu.errors import ParameterError
    from app.providers.live import FallbackMatrixRouteProvider

    class _Matrix:
        def walking_matrix(self, origin, destinations):
            raise ParameterError("routematrix", 2, "参数错误")

    class _Lite:
        def walking_matrix(self, origin, destinations):
            raise AssertionError("参数错误不应降级到 directionlite")

    prov = FallbackMatrixRouteProvider(matrix=_Matrix(), lite=_Lite())
    try:
        prov.walking_matrix(DEFAULT_CENTER, [(31.21, 121.60)])
        raise AssertionError("应当抛出 ParameterError")
    except ParameterError:
        pass


def test_direction_lite_charges_one_route_per_destination():
    from app.providers.live import DirectionLiteProvider

    class _Cli:
        def __init__(self):
            self.n = 0

        def walking_route(self, origin, destination):
            self.n += 1
            return 120.0, 200.0

    cli = _Cli()
    prov = DirectionLiteProvider(client=cli)
    out = prov.walking_matrix(DEFAULT_CENTER, [(31.21, 121.60), (31.22, 121.61), (31.23, 121.62)])
    assert out == [120.0, 120.0, 120.0]
    assert cli.n == 3, "directionlite 是 1 请求 1 条，必须逐条调用"
    assert prov.request_count == 3


# ---------------------------------------------------------------- registry
def test_registry_mode_switching():
    use_mode("mock")
    assert resolve_mode() == "mock"
    assert isinstance(get_route_provider(), MockRouteProvider)
    assert isinstance(get_place_provider(), MockPlaceProvider)

    use_mode("replay", "zhangjiang-haike")
    assert resolve_mode() == "replay"
    assert isinstance(get_route_provider(), ReplayRouteProvider)
    assert isinstance(get_place_provider(), ReplayPlaceProvider)

    use_mode("live")
    assert resolve_mode() == "live"
    assert get_route_provider().__class__.__name__ == "FallbackMatrixRouteProvider"
    use_mode("mock")


def test_registry_describe_and_communities():
    use_mode("replay", "zhangjiang-haike")
    info = describe()
    assert info["data_mode"] == "replay" and info["seed_available"] is True
    comms = list_communities()
    assert any(c["id"] == "zhangjiang-haike" for c in comms)
    use_mode("mock")


def test_registry_override_wins():
    use_mode("mock")
    from app.providers import registry

    sentinel = MockRouteProvider(center=DEFAULT_CENTER)
    registry.set_override(route=sentinel)
    assert registry.get_route_provider() is sentinel
    registry.clear_override()
    assert registry.get_route_provider() is not sentinel


# ---------------------------------------------------------------- 装饰器 / 解析模型
def test_recording_route_provider_records_requests():
    inner = MockRouteProvider(center=DEFAULT_CENTER)
    rec = RecordingRouteProvider(inner)
    dests = [xy_to_latlng(DEFAULT_CENTER, 100.0, 0.0), xy_to_latlng(DEFAULT_CENTER, 200.0, 0.0)]
    rec.walking_matrix(DEFAULT_CENTER, dests)
    assert len(rec.requests) == 1
    assert rec.requests[0]["destinations"] == [[d[0], d[1]] for d in dests]
    assert rec.responses[0] == inner.walking_matrix(DEFAULT_CENTER, dests)
    assert rec.destination_count == 2


def test_analytic_model_barrier_crossing():
    m = AnalyticModel(detour_factor=1.3, barriers=[BarrierLine(y_m=0.0, half_width_m=45.0, bridge_x_m=-700.0)])
    same = m.route_length_m((0.0, 300.0), (0.0, 500.0))
    cross = m.route_length_m((0.0, 300.0), (0.0, -300.0))
    assert abs(same - 200.0 * 1.3) < 1e-6, "同侧 = 直线距离 × 绕行系数"
    assert cross > 1400.0, f"跨河折线长度应远大于直线，实际 {cross:.0f}"
    m0 = AnalyticModel(detour_factor=1.0, barriers=[])
    assert abs(m0.route_length_m((0.0, 0.0), (0.0, 100.0)) - 100.0) < 1e-9


def test_analytic_model_effective_speed():
    m = AnalyticModel(base_speed=1.25, detour_factor=1.3)
    assert abs(m.effective_speed() - 1.25 / 1.3) < 1e-9


def test_mock_roundtrip_projection():
    lat, lng = DEFAULT_CENTER
    x, y = latlng_to_xy(DEFAULT_CENTER, lat + 0.001, lng + 0.001)
    lat2, lng2 = xy_to_latlng(DEFAULT_CENTER, x, y)
    assert abs(lat2 - (lat + 0.001)) < 1e-9
    assert abs(lng2 - (lng + 0.001)) < 1e-9
