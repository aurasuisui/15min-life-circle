"""API 层测试（CONTRACT §5）。**零网络、零配额。**

为了不让单测去调真实 LLM（每次 7~15 秒），这里把 AI 层替换成模板实现 ——
单测关心的是 API 契约，不是 LLM 输出质量。
"""
from __future__ import annotations

from tests._helpers import BACKEND, use_mode  # noqa: F401

import json

from fastapi.testclient import TestClient

from app.main import app
from app.providers import DEFAULT_CENTER
from app.schemas.report import AuditReport

client = TestClient(app)


# 说明：单测不打真实 LLM 是靠 backend/conftest.py 里的 LLM_ENABLED=false 实现的，
# **不要**在这里替换 app.services.audit.build_ai_report —— 那是模块级全局，
# 会污染其它测试文件（例如 test_audit_pipeline 里验证「app.ai 缺失时走模板」的用例）。


# ---------------------------------------------------------------- /api/health
def test_health_contract():
    use_mode("replay", "zhangjiang-haike")
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["data_mode"] == "replay"
    assert isinstance(body["llm"], bool)
    assert body["seed_available"] is True
    assert body["seed_community"] == "zhangjiang-haike"


def test_health_llm_reflects_ai_layer_not_settings():
    """回归：`llm` 必须来自 app.ai.ai_health()，不能读不存在的 settings.llm_enabled。"""
    import app.api.routes as routes

    called = {"n": 0}

    class _FakeAi:
        @staticmethod
        def ai_health():
            called["n"] += 1
            return {"llm": True, "model": "x"}

    import sys

    sys.modules["app.ai"] = _FakeAi  # type: ignore[assignment]
    try:
        assert routes._llm_enabled() is True
        assert called["n"] == 1
    finally:
        sys.modules.pop("app.ai", None)
    # 真实实现下也必须能取到（.env 里配了 LLM_API_KEY）
    assert isinstance(routes._llm_enabled(), bool)


# ---------------------------------------------------------------- /api/geocode
def test_geocode_mock_mode_is_zero_network():
    use_mode("mock")
    r = client.get("/api/geocode", params={"address": "上海市浦东新区海科路1650号"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"lat", "lng", "address"}
    assert abs(body["lat"] - DEFAULT_CENTER[0]) < 0.05
    assert abs(body["lng"] - DEFAULT_CENTER[1]) < 0.05


def test_geocode_replay_uses_snapshot():
    use_mode("replay", "zhangjiang-haike")
    r = client.get("/api/geocode", params={"address": "上海市浦东新区海科路1650号"})
    assert r.status_code == 200
    body = r.json()
    assert abs(body["lat"] - DEFAULT_CENTER[0]) < 0.01


def test_geocode_missing_address_is_422():
    use_mode("mock")
    r = client.get("/api/geocode")
    assert r.status_code == 422
    assert "error" in r.json()


# ---------------------------------------------------------------- /api/audit
def test_audit_by_lat_lng_replay():
    use_mode("replay", "zhangjiang-haike")
    r = client.get("/api/audit", params={"lat": DEFAULT_CENTER[0], "lng": DEFAULT_CENTER[1]})
    assert r.status_code == 200
    body = r.json()
    assert "_debug" not in body, "默认不应返回 _debug 段"
    AuditReport.model_validate(body)
    assert body["meta"]["data_mode"] == "replay"
    assert len(body["facilities"]["categories"]) == 8
    assert set(body["isochrones"]) == {"5", "10", "15"}


def test_audit_debug_flag_adds_debug_section():
    use_mode("mock")
    r = client.get("/api/audit", params={"lat": DEFAULT_CENTER[0], "lng": DEFAULT_CENTER[1], "debug": True})
    assert r.status_code == 200
    body = r.json()
    assert "_debug" in body
    assert "cleaning_log" in body["_debug"]
    assert isinstance(body["_debug"]["notes"], list)
    # _debug 不能污染契约字段
    AuditReport.model_validate({k: v for k, v in body.items() if not k.startswith("_")})


def test_audit_by_address():
    use_mode("replay", "zhangjiang-haike")
    r = client.get("/api/audit", params={"address": "上海市浦东新区海科路1650号"})
    assert r.status_code == 200
    assert abs(r.json()["meta"]["center"]["lat"] - DEFAULT_CENTER[0]) < 0.01


def test_audit_custom_minutes():
    use_mode("mock")
    r = client.get("/api/audit", params={"lat": DEFAULT_CENTER[0], "lng": DEFAULT_CENTER[1],
                                         "minutes": "10,15"})
    assert r.status_code == 200
    assert set(r.json()["isochrones"]) == {"10", "15"}


def test_audit_without_params_is_400():
    use_mode("mock")
    r = client.get("/api/audit")
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "INVALID_PARAMETER"
    assert "lat" in err["message"]


def test_audit_invalid_minutes_falls_back_to_default():
    use_mode("mock")
    r = client.get("/api/audit", params={"lat": DEFAULT_CENTER[0], "lng": DEFAULT_CENTER[1],
                                         "minutes": "abc"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_PARAMETER"


def test_audit_out_of_range_lat_is_422():
    use_mode("mock")
    r = client.get("/api/audit", params={"lat": 999, "lng": 121.6})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_PARAMETER"


def test_audit_mode_override_query_param():
    use_mode("mock")
    r = client.get("/api/audit", params={"lat": DEFAULT_CENTER[0], "lng": DEFAULT_CENTER[1],
                                         "mode": "replay"})
    assert r.status_code == 200
    assert r.json()["meta"]["data_mode"] == "replay"


# ---------------------------------------------------------------- /api/isochrone
def test_isochrone_endpoint():
    use_mode("mock")
    r = client.get("/api/isochrone", params={"lat": DEFAULT_CENTER[0], "lng": DEFAULT_CENTER[1],
                                             "minutes": 15})
    assert r.status_code == 200
    body = r.json()
    assert "meta" in body and "isochrone" in body
    iso = body["isochrone"]
    assert iso["target_seconds"] == 900
    assert iso["area_m2"] > 0
    ring = iso["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1]


def test_isochrone_requires_coords():
    use_mode("mock")
    r = client.get("/api/isochrone", params={"minutes": 15})
    assert r.status_code == 422


# ---------------------------------------------------------------- /api/seed/communities
def test_seed_communities():
    use_mode("replay", "zhangjiang-haike")
    r = client.get("/api/seed/communities")
    assert r.status_code == 200
    comms = r.json()["communities"]
    assert isinstance(comms, list) and comms
    first = comms[0]
    assert set(first) >= {"id", "name", "center"}
    assert first["center"]["lat"] and first["center"]["lng"]


# ---------------------------------------------------------------- /api/categories
def test_categories_frozen_contract():
    r = client.get("/api/categories")
    assert r.status_code == 200
    cats = r.json()["categories"]
    assert len(cats) == 8
    by_key = {c["key"]: c for c in cats}
    assert by_key["market"]["query"] == "农贸市场$菜市场$生鲜市场"
    assert by_key["pharmacy"]["query"] == "药店$药房"
    assert by_key["primary_school"]["query"] == "小学"
    assert by_key["clinic"]["query"] == "社区卫生服务中心$诊所$综合医院"
    assert by_key["supermarket"]["query"] == "超市$便利店"
    assert by_key["elderly_care"]["query"] == "养老院$敬老院$日间照料中心"
    assert by_key["bus_stop"]["query"] == "公交车站"
    assert by_key["park"]["query"] == "公园$广场"


# ---------------------------------------------------------------- CORS / 静态
def test_cors_allows_vite_dev_origin():
    use_mode("mock")
    r = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_root_returns_json_when_dist_missing():
    r = client.get("/")
    if r.headers.get("content-type", "").startswith("application/json"):
        body = r.json()
        assert body["service"] == "15min-life-circle"
        assert "/api/audit" in body["endpoints"]


def test_error_shape_is_contract_compliant():
    use_mode("mock")
    r = client.get("/api/audit")
    body = r.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
