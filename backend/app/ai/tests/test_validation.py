"""validation.py 单测：数值一致性校验是防幻觉的核心闸门（零网络）。"""
from __future__ import annotations

import pytest

from app.ai.validation import (
    build_metric_digest,
    build_number_set,
    extract_numbers,
    strip_clause_ids,
    validate_evidence,
    validate_numbers,
)
from app.ai.knowledge import search


# ---------------- 数字抽取 ----------------
def test_extract_plain_numbers_with_units():
    vals = {v for v, _ in extract_numbers("圈内 3 个菜市场，最近 1020 秒，覆盖率 0.18，面积 1620000 平方米")}
    assert {3.0, 1020.0, 0.18, 1620000.0} <= vals


def test_extract_ignores_dates_and_times():
    """回归（S1 现场复现的 P0）：时间戳必须整体剔除，否则每次如实回写采集时间都被误杀。"""
    cases = [
        "数据采集于 2026-10-14 14:18:21，数据模式 replay。",
        "数据采集于 2026-10-14T14:18:21+08:00。",
        "报告生成时间 2026年10月14日 14:18",
        "采集时间 2026/10/14，覆盖度 0.18。",
    ]
    for text in cases:
        vals = {v for v, _ in extract_numbers(text)}
        assert not (vals & {2026.0, 10.0, 14.0, 18.0, 21.0}), f"{text} 仍抽出时间戳数字：{vals}"
    # 同一句里的真实指标不能被一起抹掉
    vals = {v for v, _ in extract_numbers("采集时间 2026/10/14，覆盖度 0.18，菜市场 0 处。")}
    assert {0.18, 0.0} <= vals


def test_strip_dates_keeps_other_numbers():
    from app.ai.validation import strip_dates

    assert "2026" not in strip_dates("2026-10-14T14:18:21+08:00")
    assert "1020" in strip_dates("最近步行 1020 秒，采集于 2026-10-14")


def test_validate_numbers_accepts_report_timestamp(fake_report):
    """LLM 如实回写 meta 里的采集时间，不得判为幻觉。"""
    fake_report["meta"]["generated_at"] = "2026-10-14T14:18:21+08:00"
    res = validate_numbers(
        {
            "diagnosis": "本次数据采集于 2026-10-14T14:18:21+08:00，"
                         "综合得分 68.4 分，菜市场 0 个。"
        },
        fake_report,
    )
    assert res["ok"], res["reason"]


def test_validate_numbers_accepts_report_string_fields(fake_report):
    """报告字符串字段（地址门牌号、POI 名称里的编号）也是可追溯的原值。"""
    res = validate_numbers("社区地址为上海市浦东新区海科路1650号。", fake_report)
    assert res["ok"], res["reason"]


def test_extract_ignores_clause_identifiers():
    """规范编号 / 条号里的数字不能被当作指标，否则必然误报。"""
    text = "依据《城市居住区规划设计标准》GB 50180-2018 第 5.0.2 条与《上海市15分钟社区生活圈规划导则》第 6 章"
    vals = {v for v, _ in extract_numbers(text)}
    assert 50180.0 not in vals
    assert 2018.0 not in vals
    assert 5.02 not in vals


def test_strip_clause_ids_removes_standard_names():
    out = strip_clause_ids("《城市居住区规划设计标准》GB 50180-2018 第 5.0.2 条")
    assert "50180" not in out and "2018" not in out


def test_extract_chinese_numerals():
    vals = {v for v, _ in extract_numbers("建议增设两处菜市场、三所小学")}
    assert 2.0 in vals and 3.0 in vals


def test_extract_percent_and_dedup():
    vals = [v for v, _ in extract_numbers("覆盖率 82% 与 82% 相同")]
    assert vals.count(82.0) == 1


# ---------------- 数值集合 ----------------
def test_number_set_contains_report_values(fake_report):
    ns = build_number_set(fake_report)
    assert 68.4 in ns            # score.total
    assert 1620000.0 in ns       # 15min area
    assert 1020.0 in ns          # market nearest_walk_seconds
    assert 0 in ns and 15 in ns  # 结构性常量


def test_number_set_has_percent_form_for_ratio(fake_report):
    ns = build_number_set(fake_report)
    # coverage_score 0.18 应同时接受 18 / 18%
    assert 0.18 in ns and 18 in ns


def test_number_set_contains_address_house_number(fake_report):
    assert 1650 in build_number_set(fake_report), "地址门牌号应可作为合法数字回写"


def test_number_set_has_funnel_ratio(fake_report):
    ns = build_number_set(fake_report)
    # inside/deduped ≈ 69.4%
    assert any(abs(v - 186 / 268 * 100) < 0.2 for v in ns)


# ---------------- 校验器 ----------------
def test_validate_numbers_accepts_faithful_output(fake_report):
    payload = {
        "diagnosis": "该社区综合得分 68.4 分，15 分钟圈面积 1620000 平方米，菜市场 0 个。",
        "suggestions": [{"text": "建议在盲区补建菜市场，参考覆盖率 0.18。"}],
    }
    res = validate_numbers(payload, fake_report)
    assert res["ok"], res["reason"]
    assert res["mismatches"] == []


def test_validate_numbers_rejects_hallucinated_count(fake_report):
    """LLM 把菜市场数量写成 25（报告是 0，且 25 不是任何一个派生形态）→ 必须被拦住。"""
    payload = {
        "diagnosis": "该社区菜市场共 25 个，覆盖良好。",
        "suggestions": [{"text": "维持现状"}],
    }
    res = validate_numbers(payload, fake_report)
    assert not res["ok"]
    assert any(m["value"] == 25.0 for m in res["mismatches"])
    assert "数值一致性校验失败" in res["reason"]


def test_percent_form_is_intentionally_admitted(fake_report):
    """已知取舍：评分 0.12 允许写成 12%，因此「12」本身不能作为幻觉判据。

    这正是校验器必须配合提示词（强制引用指标）使用的原因；
    校验器负责拦「凭空数字」，不负责语义层面的单位误用。
    """
    assert 12.0 in build_number_set(fake_report)   # 来自 coverage_score 0.12 的百分比形态
    assert 25.0 not in build_number_set(fake_report)


def test_validate_numbers_rejects_tampered_score_and_area(fake_report):
    """篡改综合得分（68.4 -> 91.2）与 15 分钟圈面积（1620000 -> 3200000）都必须被拦住。"""
    payload = {
        "diagnosis": "综合得分 91.2 分，15 分钟圈面积 3200000 平方米。",
        "suggestions": [{"text": "保持"}],
    }
    res = validate_numbers(payload, fake_report)
    assert not res["ok"]
    bad = {m["value"] for m in res["mismatches"]}
    assert 91.2 in bad and 3200000.0 in bad


def test_validate_numbers_allows_percent_writing(fake_report):
    res = validate_numbers("菜市场覆盖率 18%，超市便利店覆盖率 88%", fake_report)
    assert res["ok"], res["reason"]


def test_validate_numbers_allows_clause_citation(fake_report):
    res = validate_numbers(
        "依据《城市居住区规划设计标准》GB 50180-2018 第 5.0.2 条，服务半径不宜大于 1000 m。",
        fake_report,
    )
    assert res["ok"], res["reason"]


def test_validate_numbers_reports_reason_when_empty(fake_report):
    res = validate_numbers({}, fake_report)
    assert res["ok"]  # 无数字即无谎言


def test_validate_numbers_allows_numbers_from_injected_clauses(fake_report):
    """忠实引用条文里的规范数值（服务半径 500 m / 300 m）不算幻觉。"""
    clauses = [c.text for c in search(["market", "primary_school"], k=5)]
    payload = {
        "diagnosis": "依据规范，菜市场服务半径宜控制在 500 m 以内，幼儿园服务半径不宜大于 300 m；"
                     "本社区菜市场 0 个、覆盖率 0.18，未达标。"
    }
    without = validate_numbers(payload, fake_report)
    with_clauses = validate_numbers(payload, fake_report, clauses=clauses)
    assert not without["ok"], "未注入条文时，500/300 应被视为无依据数字"
    assert with_clauses["ok"], with_clauses["reason"]


def test_validate_numbers_still_rejects_numbers_absent_from_clauses(fake_report):
    """放行条文数字不等于放行一切：条文里没有的数字依然要被拦住。"""
    clauses = [c.text for c in search(["market", "primary_school"], k=5)]
    payload = {"diagnosis": "本社区共有 47 处菜市场、61 所小学，服务半径 500 m。"}
    res = validate_numbers(payload, fake_report, clauses=clauses)
    assert not res["ok"]
    bad = {m["value"] for m in res["mismatches"]}
    assert 47.0 in bad and 61.0 in bad
    assert 500.0 not in bad, "条文里存在的 500 不应被判为越界"


# ---------------- 出处校验 ----------------
def test_validate_evidence_accepts_retrieved_sources(fake_report):
    clauses = search(["market"], k=3)
    ev = [{"clause": "x", "source": c.source} for c in clauses]
    assert validate_evidence(ev, clauses)["ok"]


def test_validate_evidence_rejects_fabricated_source():
    clauses = search(["market"], k=3)
    ev = [{"clause": "x", "source": "《中华人民共和国不存在法》第 1 条"}]
    res = validate_evidence(ev, clauses)
    assert not res["ok"]
    assert res["unknown"]


# ---------------- 指标摘要 ----------------
def test_metric_digest_shape(fake_report):
    d = build_metric_digest(fake_report)
    assert d["score_total"] == 68.4 and d["score_grade"] == "C"
    assert len(d["isochrones"]) == 3
    assert d["isochrones"][0]["minutes"] == 5.0
    assert len(d["categories"]) == 8
    assert d["blindspot_total_area_m2"] == 185000.0
    assert d["categories"][0]["top_pois"][0]["name"] == "张江农贸市场"


def test_metric_digest_tolerates_empty_report():
    d = build_metric_digest({})
    assert d["score_total"] is None
    assert d["categories"] == [] and d["isochrones"] == []
