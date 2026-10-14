"""pytest 公共夹具：全部**零网络**。

- `fake_report`：一份符合 CONTRACT.md 4 的最小完整报告（含 1 个菜市场盲区、1 个小学盲区）；
- `ai_report`：断言用的 Schema 辅助函数；
- 显式清理 LLM_* 环境变量，保证用例之间互不污染。
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

# 保证 `import app.ai` 可用（pytest.ini 里已设 pythonpath=.，这里再兜一层）
BACKEND = Path(__file__).resolve().parents[3]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

#: 仓库根目录（backend 的上一级）
REPO_ROOT = BACKEND.parent

_FAKE_REPORT: dict = {
    "meta": {
        "center": {"lat": 31.2045, "lng": 121.5990},
        "address": "上海市浦东新区海科路1650号",
        "generated_at": "2026-10-14T15:00:00+08:00",
        "data_mode": "replay",
        "degraded": False,
        "degrade_reasons": [],
        "quota": {"routes_used": 432, "routes_limit": 5000, "poi_used": 3, "poi_limit": 100},
    },
    "isochrones": {
        "5": {"target_seconds": 300, "area_m2": 210000.0, "n_samples": 96,
              "routes_charged": 96, "uncertainty_band_ratio": 0.12,
              "geometry": {"type": "Polygon", "coordinates": []}},
        "10": {"target_seconds": 600, "area_m2": 780000.0, "n_samples": 288,
               "routes_charged": 288, "uncertainty_band_ratio": 0.15,
               "geometry": {"type": "Polygon", "coordinates": []}},
        "15": {"target_seconds": 900, "area_m2": 1620000.0, "n_samples": 432,
               "routes_charged": 432, "uncertainty_band_ratio": 0.18,
               "geometry": {"type": "Polygon", "coordinates": []}},
    },
    "facilities": {
        "categories": [
            {"key": "market", "name": "菜市场", "query": "农贸市场$菜市场$生鲜市场",
             "count": 0, "density_per_km2": 0.0, "nearest_walk_seconds": 1020,
             "coverage_score": 0.18,
             "pois": [{"uid": "m1", "name": "张江农贸市场", "lat": 31.21, "lng": 121.61,
                       "address": "张江路", "tag": "市场", "walk_seconds": 1020}]},
            {"key": "pharmacy", "name": "药店", "query": "药店$药房",
             "count": 3, "density_per_km2": 1.85, "nearest_walk_seconds": 240,
             "coverage_score": 0.71,
             "pois": [{"uid": "p1", "name": "华氏大药房", "lat": 31.2, "lng": 121.6,
                       "address": "海科路", "tag": "药店", "walk_seconds": 240}]},
            {"key": "primary_school", "name": "小学", "query": "小学",
             "count": 0, "density_per_km2": 0.0, "nearest_walk_seconds": 1450,
             "coverage_score": 0.12,
             "pois": [{"uid": "s1", "name": "张江镇中心小学", "lat": 31.22, "lng": 121.6,
                       "address": "张江路", "tag": "小学", "walk_seconds": 1450}]},
            {"key": "clinic", "name": "社区医疗", "query": "社区卫生服务中心$诊所$综合医院",
             "count": 1, "density_per_km2": 0.62, "nearest_walk_seconds": 600,
             "coverage_score": 0.55, "pois": []},
            {"key": "supermarket", "name": "超市便利店", "query": "超市$便利店",
             "count": 5, "density_per_km2": 3.09, "nearest_walk_seconds": 150,
             "coverage_score": 0.88, "pois": []},
            {"key": "elderly_care", "name": "养老设施", "query": "养老院$敬老院$日间照料中心",
             "count": 1, "density_per_km2": 0.62, "nearest_walk_seconds": 780,
             "coverage_score": 0.42, "pois": []},
            {"key": "bus_stop", "name": "公交站", "query": "公交车站",
             "count": 7, "density_per_km2": 4.32, "nearest_walk_seconds": 120,
             "coverage_score": 0.93, "pois": []},
            {"key": "park", "name": "公园绿地", "query": "公园$广场",
             "count": 2, "density_per_km2": 1.23, "nearest_walk_seconds": 420,
             "coverage_score": 0.66, "pois": []},
        ],
        "cleaning_funnel": {"raw": 320, "deduped": 268, "corrected": 254, "inside": 186},
    },
    "blindspots": {
        "total_area_m2": 185000.0,
        "polygons": [
            {"area_m2": 120000.0, "missing": ["market", "primary_school"],
             "sample_point": {"lat": 31.21, "lng": 121.61},
             "geometry": {"type": "Polygon", "coordinates": []}},
            {"area_m2": 65000.0, "missing": ["market"],
             "sample_point": {"lat": 31.215, "lng": 121.605},
             "geometry": {"type": "Polygon", "coordinates": []}},
        ],
    },
    "score": {
        "total": 68.4,
        "grade": "C",
        "dimensions": [
            {"key": "medical", "name": "医疗", "score": 8.2, "weight": 0.15, "raw_count": 5},
            {"key": "commerce", "name": "购物", "score": 7.1, "weight": 0.2, "raw_count": 5},
            {"key": "education", "name": "教育", "score": 2.4, "weight": 0.15, "raw_count": 0},
            {"key": "elderly", "name": "养老", "score": 5.6, "weight": 0.15, "raw_count": 1},
            {"key": "transit", "name": "交通", "score": 9.1, "weight": 0.1, "raw_count": 7},
            {"key": "green", "name": "绿地", "score": 6.8, "weight": 0.1, "raw_count": 2},
            {"key": "accessibility", "name": "可达性", "score": 7.4, "weight": 0.1, "raw_count": 0},
            {"key": "balance", "name": "均衡性", "score": 5.2, "weight": 0.05, "raw_count": 0},
        ],
    },
    "siting": [
        {"lat": 31.21, "lng": 121.61, "reason": "该点 1km 内无菜市场", "gains": {"market": 1}},
        {"lat": 31.215, "lng": 121.605, "reason": "该点 1km 内无小学",
         "gains": {"primary_school": 1}},
    ],
}

#: LLM 相关环境变量（测试中统一清理，避免真实 .env 干扰）
LLM_ENV_KEYS = (
    "LLM_ENABLED", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
    "LLM_EFFORT", "LLM_TIMEOUT_S", "LLM_MAX_TOKENS", "LLM_MAX_RETRIES", "LLM_TEMPERATURE",
)


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认清空 LLM_* 环境变量（含 .env 兜底），保证用例不依赖真实凭据。"""
    for key in LLM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    import app.ai.client as client_mod
    monkeypatch.setattr(client_mod, "_ENV_FILE_CACHE", {}, raising=False)


@pytest.fixture
def fake_report() -> dict:
    """一份契约完整的假报告（深拷贝，用例可安全修改）。"""
    return copy.deepcopy(_FAKE_REPORT)


@pytest.fixture
def report_factory():
    """按需定制报告的工厂。"""
    def _make(**overrides) -> dict:
        base = copy.deepcopy(_FAKE_REPORT)
        for path, value in overrides.items():
            node = base
            parts = path.split("__")
            for p in parts[:-1]:
                node = node[p]
            node[parts[-1]] = value
        return base
    return _make


@pytest.fixture
def llm_env(monkeypatch: pytest.MonkeyPatch):
    """一键注入可用的 LLM 环境变量（不发起真实请求）。"""
    def _set(**kwargs) -> None:
        env = {
            "LLM_ENABLED": "true",
            "LLM_BASE_URL": "https://api.deepseek.com",
            "LLM_API_KEY": "sk-test-not-a-real-key",
            "LLM_MODEL": "deepseek-flash",
            "LLM_EFFORT": "low",
            "LLM_MAX_TOKENS": "4000",
            "LLM_MAX_RETRIES": "0",
        }
        env.update({k: str(v) for k, v in kwargs.items()})
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    return _set


def assert_ai_report_schema(obj: dict, *, expect_degraded: bool | None = None) -> None:
    """断言 ai_report 子对象符合 CONTRACT.md 4。"""
    assert isinstance(obj, dict), "ai_report 必须是 dict"
    assert "degraded" in obj, "必须包含 degraded 布尔字段"
    assert isinstance(obj["degraded"], bool)
    if expect_degraded is not None:
        assert obj["degraded"] is expect_degraded
    assert isinstance(obj.get("diagnosis"), str) and obj["diagnosis"].strip(), "diagnosis 不能为空"
    assert isinstance(obj.get("evidence"), list), "evidence 必须是数组"
    for e in obj["evidence"]:
        assert set(e.keys()) >= {"clause", "source"}
        assert isinstance(e["clause"], str) and isinstance(e["source"], str)
    assert isinstance(obj.get("suggestions"), list) and obj["suggestions"], "suggestions 不能为空"
    for s in obj["suggestions"]:
        assert set(s.keys()) >= {"text", "priority"}
        assert isinstance(s["text"], str) and s["text"].strip()
        assert s["priority"] in ("high", "medium", "low")
    # 序列化安全（API 直接返回 JSON）
    json.dumps(obj, ensure_ascii=False)
