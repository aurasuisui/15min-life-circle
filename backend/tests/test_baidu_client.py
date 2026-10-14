"""BaiduClient 单元测试：SN 签名 / 错误分类 / 令牌桶 / 配额账本 / 缓存。**零网络。**"""
from __future__ import annotations

from tests._helpers import BACKEND  # noqa: F401  (确保 sys.path 就绪)

import time

from app.baidu.cache import FileCache, cache_key, quantize
from app.baidu.client import ENDPOINTS, BaiduClient, endpoint_available, mark_unavailable, reset_endpoints
from app.baidu.errors import (
    KIND_AK,
    KIND_PARAM,
    KIND_PERMISSION,
    KIND_QPS,
    KIND_QUOTA,
    KIND_SN,
    QuotaExhaustedError,
    TransientApiError,
    classify,
    to_exception,
)
from app.baidu.quota import QuotaLedger
from app.baidu.ratelimit import TokenBucket, get_bucket, reset_buckets
from app.baidu.sign import OFFICIAL_VECTOR_SN, bd_encode, make_sn, verify_sign_vector


# ---------------------------------------------------------------- SN 签名
def test_official_sn_vector():
    """官方测试向量：不需要任何真实 AK 就能验证签名实现。"""
    assert verify_sign_vector() is True
    got = make_sn("/geocoder/v2/", {"address": "百度大厦", "output": "json", "ak": "yourak"}, "yoursk")
    assert got == OFFICIAL_VECTOR_SN, got


def test_sn_is_double_encoded():
    """必须「value 编码一次 + 整串再编码一次」；漏掉第二次是 211 最常见的成因。"""
    import hashlib

    params = {"address": "百度大厦", "output": "json", "ak": "yourak"}
    once = "&".join(f"{k}={bd_encode(v)}" for k, v in params.items())
    single = hashlib.md5(f"/geocoder/v2/?{once}yoursk".encode()).hexdigest()
    double = make_sn("/geocoder/v2/", params, "yoursk")
    assert single != double, "单次编码与官方双重编码算法不应相同"


def test_bd_encode_matches_official_charset():
    assert bd_encode("a-b_c.d~e") == "a-b_c.d~e"
    assert bd_encode(" ") == "%20"          # 空格必须是 %20，不是 +
    assert bd_encode("/") == "%2F"
    assert bd_encode("百度") == "%E7%99%BE%E5%BA%A6"


def test_sn_param_order_matters():
    a = make_sn("/place/v3/around", {"query": "药店", "ak": "k"}, "s")
    b = make_sn("/place/v3/around", {"ak": "k", "query": "药店"}, "s")
    assert a != b, "参数顺序不同 -> 签名必须不同（URL 顺序即签名顺序）"


# ---------------------------------------------------------------- 错误分类
def test_classify_quota_variants():
    assert classify("place", 4, "Quota Failure").kind == KIND_QUOTA
    assert classify("route", 302, "天配额超限").kind == KIND_QUOTA
    assert classify("place", 0, "") is None, "status=0 是成功，不是错误"


def test_classify_qps_and_transient():
    assert classify("place", 401, "并发超限").kind == KIND_QPS
    assert classify("route", 401, "too many requests").retryable is True
    assert classify("place", 500, "server error").retryable is True
    assert classify("place", 4, "quota").retryable is False, "配额耗尽绝不重试"


def test_classify_ak_conflict_between_services():
    """不同服务状态码语义冲突：routematrix 用 2 表示 ak is invalid。"""
    assert classify("route", 2, "ak is invalid").kind == KIND_AK
    assert classify("place", 2, "参数错误").kind == KIND_PARAM
    assert classify("place", 200, "APP不存在，AK有误请检查再重试").kind == KIND_AK


def test_classify_sn_and_permission():
    assert classify("route", 211, "APP SN校验失败").kind == KIND_SN
    assert classify("place", 240, "服务被禁用").kind == KIND_PERMISSION
    assert classify("place", 210, "IP白名单校验失败").kind != KIND_SN


def test_to_exception_mapping():
    e = to_exception("place", 302, "quota")
    assert isinstance(e, QuotaExhaustedError)
    assert e.retryable is False
    t = to_exception("place", 500, "boom")
    assert isinstance(t, TransientApiError) and t.retryable is True


# ---------------------------------------------------------------- 配额账本
def test_ledger_reserve_commit_release():
    led = QuotaLedger(db_path=None, limits={"place": 3, "route": 10})
    led.reserve("place", 2)
    assert led.snapshot()["services"]["place"]["reserved"] == 2
    led.commit("place", 2)
    assert led.used("place") == 2
    led.reserve("place", 1)
    led.release("place", 1)
    assert led.snapshot()["services"]["place"]["reserved"] == 0
    try:
        led.reserve("place", 5)
        raise AssertionError("应当抛 QuotaExhaustedError（快速失败）")
    except QuotaExhaustedError:
        pass


def test_ledger_levels_and_persistence():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "quota.db"
        led = QuotaLedger(db_path=str(db), limits={"place": 100, "route": 5000})
        led.charge("place", 80)
        assert led.snapshot()["services"]["place"]["level"] == "yellow"
        led.charge("place", 15)
        assert led.snapshot()["services"]["place"]["level"] == "red"
        led2 = QuotaLedger(db_path=str(db), limits={"place": 100, "route": 5000})
        try:
            assert led2.used("place") == 95, "账本必须落盘，重启不丢"
            led2.mark_exhausted("place")
            assert led2.snapshot()["services"]["place"]["level"] == "exhausted"
        finally:
            led2.close()
        led.close()


def test_ledger_snapshot_contract_fields():
    led = QuotaLedger(db_path=None, limits={"place": 100, "route": 5000})
    snap = led.snapshot()
    for key in ("routes_used", "routes_limit", "poi_used", "poi_limit"):
        assert key in snap, f"CONTRACT §4 要求 meta.quota 含 {key}"


# ---------------------------------------------------------------- 令牌桶
def test_token_bucket_rate():
    reset_buckets()
    b = TokenBucket(rate=50.0, burst=1, name="t")
    t0 = time.monotonic()
    for _ in range(3):
        b.acquire()
    dt = time.monotonic() - t0
    assert dt >= 0.03, f"3 次取令牌应至少花 2/50 秒，实际 {dt:.3f}"


def test_token_bucket_shared_per_service():
    reset_buckets()
    a = get_bucket("place", rate=2.5, burst=3)
    b = get_bucket("place", rate=2.5, burst=3)
    assert a is b, "同一服务的桶必须进程级共享，否则并发时会双双突破 QPS"


# ---------------------------------------------------------------- 缓存
def test_cache_key_quantizes_coordinates():
    k1 = cache_key("route", {"origin": [31.204500001, 121.599000002]})
    k2 = cache_key("route", {"origin": [31.204500004, 121.599000003]})
    assert k1 == k2, "≈1 m 量化后应命中同一个键"


def test_cache_roundtrip_and_ttl():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        c = FileCache(td, backend="sqlite")
        c.set("k1", "place", {"a": 1}, ttl_s=100)
        assert c.get("k1") == {"a": 1}
        c.set("k2", "place", {"b": 2}, ttl_s=-1)
        assert c.get("k2") is None, "过期条目必须失效"
        stats = c.stats()
        assert stats["hits"] >= 1 and stats["backend"] == "sqlite"


def test_cache_disabled_is_noop():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        c = FileCache(td, enabled=False)
        c.set("k", "place", {"a": 1})
        assert c.get("k") is None


def test_quantize_recurses():
    assert quantize({"a": [1.234567891, {"b": 2.0}]}) == {"a": [1.23457, {"b": 2.0}]}


# ---------------------------------------------------------------- 客户端（零网络）
class FailingHttp:
    """任何真实 HTTP 调用都会失败 —— 用来证明「缓存命中时根本没发请求」。"""

    def get(self, *a, **k):
        raise AssertionError("缓存命中时不应发起任何 HTTP 请求")


def test_client_uses_cache_before_quota():
    """缓存命中 -> 不消耗配额、不发网络请求。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        cache = FileCache(td, backend="sqlite")
        led = QuotaLedger(db_path=":memory:", limits={"place": 100})
        cli = BaiduClient(ak="fake", sk="", ledger=led, cache=cache, http_client=FailingHttp())
        # 缓存键必须与 BaiduClient.get() 内部完全一致：service:endpoint + 全部参数
        params = {
            "query": "药店", "location": "31.2,121.6", "radius": 1500,
            "radius_limit": "true", "page_size": 20, "page_num": 0, "scope": 2, "coord_type": 3,
        }
        cache.set(cache_key("place:place", params), "place", {"status": 0, "results": []})
        out = cli.place_around("药店", (31.2, 121.6))
        assert out["status"] == 0
        assert led.used("place") == 0, "命中缓存不得消耗配额"
        assert cli.calls.get("cache_hit") == 1


def test_client_quota_exhausted_fails_fast():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        led = QuotaLedger(db_path=":memory:", limits={"place": 1})
        cli = BaiduClient(ak="fake", sk="", ledger=led, cache=FileCache(td, enabled=False))
        led.mark_exhausted("place")
        try:
            cli.place_around("药店", (31.2, 121.6))
            raise AssertionError("配额耗尽必须快速失败")
        except QuotaExhaustedError:
            pass


def test_endpoint_circuit_breaker():
    reset_endpoints()
    assert endpoint_available("routematrix") is True
    mark_unavailable("routematrix")
    assert endpoint_available("routematrix") is False
    reset_endpoints()
    assert endpoint_available("routematrix") is True


def test_endpoint_paths_match_contract():
    assert ENDPOINTS["place"][0] == "/place/v3/around"
    assert ENDPOINTS["routematrix"][0] == "/routematrix/v2/walking"
    assert ENDPOINTS["directionlite"][0] == "/directionlite/v1/walking"
    assert ENDPOINTS["geocode"][0] == "/geocoding/v3/"
