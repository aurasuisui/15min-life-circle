"""knowledge.py 单测：知识库完整性 + 检索可用性（零网络）。"""
from __future__ import annotations

import pytest

from app.ai.knowledge import (
    CATEGORY_TO_FEATURE,
    CLAUSES,
    FEATURE_KEYS,
    citation_set,
    format_clauses,
    get_clause,
    search,
)

STANDARD_CODES = {"GB50180", "WZSB", "SH15", "SWB"}


def test_four_standards_covered():
    """四份规范文件都必须有条文入库。"""
    codes = {c.code for c in CLAUSES}
    assert codes == STANDARD_CODES, f"缺少标准：{STANDARD_CODES - codes}"


def test_clause_fields_complete_and_unique():
    ids = [c.id for c in CLAUSES]
    assert len(ids) == len(set(ids)), "条文 id 必须唯一"
    assert len(CLAUSES) >= 20
    for c in CLAUSES:
        assert c.id and "#" in c.id
        assert len(c.text) >= 30, f"{c.id} 条文过短"
        assert c.source.startswith("《") or c.source.startswith("商务部"), f"{c.id} 出处不规范"
        assert c.features, f"{c.id} 未标注可服务的设施类别"
        assert c.features <= set(FEATURE_KEYS), f"{c.id} 含未知 feature"


def test_every_category_has_clause():
    """8 类设施都必须至少有一条可引用条文（RAG 覆盖度）。"""
    for key in CATEGORY_TO_FEATURE:
        hits = [c for c in CLAUSES if key in c.features]
        assert hits, f"{key} 无任何规范条文支撑"


@pytest.mark.parametrize("key", ["market", "pharmacy", "primary_school", "clinic",
                                 "supermarket", "elderly_care", "bus_stop", "park"])
def test_search_returns_relevant_clause(key):
    got = search([key], k=3)
    assert got, "检索结果不能为空"
    assert any(key in c.features for c in got), f"{key} 的检索结果中没有该类条文"


def test_search_never_empty_even_for_unknown_feature():
    assert search(["not_a_real_key"], k=3)
    assert search([], k=3)
    assert search(None, query="", k=3)


def test_search_is_stable_and_sorted():
    a = [c.id for c in search(["elderly_care"], k=5)]
    b = [c.id for c in search(["elderly_care"], k=5)]
    assert a == b, "同输入必须可复现"


def test_search_respects_k():
    assert len(search(["market"], k=2)) <= 2


def test_format_clauses_contains_source_and_is_bounded():
    blocks = format_clauses(search(["market"], k=4), max_chars=100000)
    assert "出处：" in blocks
    assert "GB 50180" in blocks or "上海市15分钟" in blocks
    small = format_clauses(search(["market"], k=20), max_chars=200)
    assert len(small) <= 400  # 截断生效


def test_citation_set_covers_ids_and_standards():
    cs = citation_set(search(["park"], k=3))
    for c in search(["park"], k=3):
        assert c.id in cs and c.standard in cs and c.source in cs


def test_get_clause_lookup():
    assert get_clause("SH15#3.2") is not None
    assert get_clause("NOPE#1") is None
