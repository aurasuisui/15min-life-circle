"""零第三方依赖冒烟测试 —— **必须能被 `scripts/selftest.py` 直接跑通**。

约束（selftest.py 的 pytest shim 只提供 approx / raises / fixture / mark）：
  * 只用普通函数与 assert，**不要**用 pytest fixture（tmp_path / monkeypatch / capsys）
  * **不要**用参数化、异步、skip 标记
  * 只用标准库 + 仓库自带的 app 包

覆盖范围：SN 签名的官方测试向量、错误分类、配额账本、缓存、mock/replay provider、
POI 清洗漏斗、盲区识别、评分模型、以及一次 mock 模式的全链路 audit。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
SEED_DIR = BACKEND.parent / "data" / "seed"

# mock 模式 = 零网络、零配额、确定性
import os

os.environ["DATA_MODE"] = "mock"
os.environ["SEED_DIR"] = str(SEED_DIR)
os.environ["LLM_ENABLED"] = "false"      # 冒烟测试不打真实 LLM

from app.baidu.cache import FileCache, cache_key                      # noqa: E402
from app.baidu.client import BaiduClient                              # noqa: E402
from app.baidu.errors import (                                        # noqa: E402
    KIND_AK, KIND_PARAM, KIND_QPS, KIND_QUOTA, KIND_SN,
    QuotaExhaustedError, classify,
)
from app.baidu.quota import QuotaLedger                               # noqa: E402
from app.baidu.ratelimit import TokenBucket, get_bucket, reset_buckets  # noqa: E402
from app.baidu.sign import OFFICIAL_VECTOR_SN, bd_encode, make_sn, verify_sign_vector  # noqa: E402
from app.constants import BLIND_SPOT_DISTANCE_M, CATEGORY_KEYS        # noqa: E402
from app.providers import (                                           # noqa: E402
    DEFAULT_CENTER, MockPlaceProvider, MockRouteProvider, get_route_provider, resolve_mode,
)
from app.providers.replay import ReplayBundle, find_community_dir, route_key  # noqa: E402
from app.schemas.report import AuditReport                            # noqa: E402
from app.services.audit import AuditOptions, run_audit                # noqa: E402
from app.services.score import grade_of                               # noqa: E402


# ---------------------------------------------------------------- SN 签名
def test_offline_sn_official_vector():
    assert verify_sign_vector() is True
    assert make_sn("/geocoder/v2/", {"address": "百度大厦", "output": "json", "ak": "yourak"},
                   "yoursk") == OFFICIAL_VECTOR_SN


def test_offline_sn_encoding_rules():
    assert bd_encode(" ") == "%20"
    assert bd_encode("百度") == "%E7%99%BE%E5%BA%A6"
    assert make_sn("/p/", {"a": "1", "b": "2"}, "sk") != make_sn("/p/", {"b": "2", "a": "1"}, "sk")


# ---------------------------------------------------------------- 错误分类
def test_offline_error_classification():
    assert classify("place", 0, "") is None
    assert classify("place", 302, "Quota Failure").kind == KIND_QUOTA
    assert classify("place", 302, "quota").retryable is False
    assert classify("place", 401, "并发超限").kind == KIND_QPS
    assert classify("route", 2, "ak is invalid").kind == KIND_AK
    assert classify("place", 2, "参数错误").kind == KIND_PARAM
    assert classify("route", 211, "APP SN校验失败").kind == KIND_SN


# ---------------------------------------------------------------- 配额账本
def test_offline_quota_ledger_fast_fail():
    led = QuotaLedger(db_path=":memory:", limits={"place": 3, "route": 10})
    led.charge("place", 2)
    assert led.used("place") == 2
    assert led.snapshot()["poi_limit"] == 3
    try:
        led.reserve("place", 5)
        raise AssertionError("配额不足必须快速失败")
    except QuotaExhaustedError:
        pass
    led.close()


def test_offline_quota_levels():
    led = QuotaLedger(db_path=":memory:", limits={"place": 100})
    led.charge("place", 80)
    assert led.snapshot()["services"]["place"]["level"] == "yellow"
    led.charge("place", 15)
    assert led.snapshot()["services"]["place"]["level"] == "red"
    led.close()


# ---------------------------------------------------------------- 令牌桶 / 缓存
def test_offline_token_bucket():
    reset_buckets()
    a = get_bucket("place", rate=1000.0, burst=3)
    b = get_bucket("place", rate=1000.0, burst=3)
    assert a is b
    t = TokenBucket(rate=1000.0, burst=1, name="x")
    assert t.acquire() >= 0.0


def test_offline_cache_key_and_memory_only():
    assert cache_key("route", {"o": [31.204500001, 121.6]}) == cache_key("route", {"o": [31.204500004, 121.6]})
    c = FileCache("__no_disk__", enabled=False)
    c.set("k", "place", {"a": 1})
    assert c.get("k") is None


def test_offline_client_fails_fast_when_quota_exhausted():
    led = QuotaLedger(db_path=":memory:", limits={"place": 1})
    led.mark_exhausted("place")
    cli = BaiduClient(ak="fake", sk="", ledger=led, cache=FileCache("__no_disk__", enabled=False))
    try:
        cli.place_around("药店", (31.2, 121.6))
        raise AssertionError("配额耗尽必须快速失败（不重试、不联网）")
    except QuotaExhaustedError:
        pass
    led.close()


# ---------------------------------------------------------------- provider
def test_offline_mock_provider_deterministic_and_monotone():
    p = MockRouteProvider(center=DEFAULT_CENTER)
    from app.providers.mock import xy_to_latlng

    dests = [xy_to_latlng(DEFAULT_CENTER, 0.0, r) for r in (200, 400, 800)]
    a = p.walking_matrix(DEFAULT_CENTER, dests)
    b = p.walking_matrix(DEFAULT_CENTER, dests)
    assert a == b
    assert a[0] < a[1] < a[2]


def test_offline_mock_place_has_dirty_data():
    p = MockPlaceProvider(center=DEFAULT_CENTER)
    d = p.search("农贸市场$菜市场$生鲜市场$超市$便利店$小学$公交车站$公园$广场", DEFAULT_CENTER)
    names = [r["name"] for r in d["results"]]
    assert any("幼儿园" in n for n in names)
    assert len([r for r in d["results"] if r["uid"] == "MK12"]) == 2


def test_offline_registry_mock_mode():
    os.environ["DATA_MODE"] = "mock"
    assert resolve_mode() == "mock"
    assert isinstance(get_route_provider(), MockRouteProvider)


# ---------------------------------------------------------------- seed 快照
def test_offline_seed_snapshot_present():
    d = find_community_dir(SEED_DIR, "zhangjiang-haike")
    b = ReplayBundle.load(d)
    assert b.index.get("center")
    assert len(b.poi) == 2
    assert len(b.routes) >= 10
    key = next(iter(b.routes))
    rec = b.routes[key]
    assert key == route_key(tuple(rec["origin"]), [tuple(x) for x in rec["destinations"]])


# ---------------------------------------------------------------- 全链路
def test_offline_full_audit_pipeline():
    os.environ["DATA_MODE"] = "mock"
    from app.providers import registry

    registry.reset_state()
    out = run_audit(AuditOptions())
    body = {k: v for k, v in out.report.items() if not k.startswith("_")}
    AuditReport.model_validate(body)          # 契约校验

    iso = out.report["isochrones"]
    assert set(iso) == {"5", "10", "15"}
    areas = [iso[k]["area_m2"] for k in ("5", "10", "15")]
    assert areas[0] < areas[1] < areas[2]
    assert len(set(iso[k]["routes_charged"] for k in iso)) == 1, "三档共享一次采样"

    funnel = out.report["facilities"]["cleaning_funnel"]
    assert funnel["raw"] > funnel["deduped"] >= funnel["corrected"] >= funnel["inside"] > 0
    assert len(out.report["facilities"]["categories"]) == 8
    assert [c["key"] for c in out.report["facilities"]["categories"]] == list(CATEGORY_KEYS)

    assert out.report["blindspots"]["total_area_m2"] > 0, "样例社区刻意留有小学盲区"
    assert out.report["score"]["grade"] in ("A", "B", "C", "D")
    assert len(out.report["score"]["dimensions"]) == 8
    for key in ("degraded", "diagnosis", "evidence", "suggestions"):
        assert key in out.report["ai_report"]


def test_offline_blindspot_uses_distance_semantics():
    from app.providers.barriers import zhangjiang_model
    from app.services.accessibility import effective_distance

    assert abs(BLIND_SPOT_DISTANCE_M - 1000.0) < 1e-9
    m = zhangjiang_model()
    assert m.barriers and m.barriers[0].half_width_m > 0
    del effective_distance


def test_offline_grade_bands():
    assert grade_of(90) == "A" and grade_of(75) == "B" and grade_of(60) == "C" and grade_of(1) == "D"
