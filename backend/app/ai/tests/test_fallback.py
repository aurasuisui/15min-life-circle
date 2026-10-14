"""fallback.py 单测：无 Key / 超时 / 校验失败时的功能保底（零网络）。"""
from __future__ import annotations

from app.ai.fallback import _template_allowed, build_fallback_report
from app.ai.knowledge import search
from app.ai.validation import validate_numbers

from conftest import assert_ai_report_schema


def test_fallback_schema_and_degraded(fake_report):
    out = build_fallback_report(fake_report, reason="单元测试")
    assert_ai_report_schema(out, expect_degraded=True)
    assert out["generator"] == "template"
    assert out["degrade_reason"] == "单元测试"


def test_fallback_cites_real_clauses(fake_report):
    """模板也必须引用知识库里的真实条文，不能凭空造出处。"""
    out = build_fallback_report(fake_report)
    assert out["evidence"], "模板必须给出依据条文"
    allowed = {c.source for c in search(["_overall"], k=10)} | {
        c.source for c in search(["market", "primary_school"], k=10)
    }
    for e in out["evidence"]:
        assert e["source"] in allowed, f"出处不在知识库中：{e['source']}"


def test_fallback_numbers_all_traceable(fake_report):
    """模板自检必须通过：模板里的每个数字都能在报告里找到。"""
    out = build_fallback_report(fake_report)
    assert out["numeric_check"]["ok"], out["numeric_check"]["mismatches"]
    # 与 build_fallback_report 内部使用同一套放行规则（阈值常量 + 精确单位换算）
    chk = validate_numbers(
        {"diagnosis": out["diagnosis"], "suggestions": [s["text"] for s in out["suggestions"]]},
        fake_report,
        allowed=_template_allowed(fake_report),
    )
    assert chk["ok"], chk["reason"]


def test_fallback_mentions_shortage_and_blindspot(fake_report):
    out = build_fallback_report(fake_report)
    text = out["diagnosis"] + "".join(s["text"] for s in out["suggestions"])
    assert "菜市场" in text and "小学" in text, "必须点名真实缺口类别"
    assert "盲区" in text
    assert "68.4" in out["diagnosis"], "必须回写综合得分"


def test_fallback_priorities_sorted_and_valid(fake_report):
    out = build_fallback_report(fake_report)
    order = {"high": 0, "medium": 1, "low": 2}
    ranks = [order[s["priority"]] for s in out["suggestions"]]
    assert ranks == sorted(ranks), "建议必须按 priority 排序"
    assert ranks[0] == 0, "存在缺口时必须有 high 优先级建议"


def test_fallback_high_priority_for_zero_count(fake_report):
    out = build_fallback_report(fake_report)
    highs = [s["text"] for s in out["suggestions"] if s["priority"] == "high"]
    assert any("菜市场" in t for t in highs)
    assert any("小学" in t for t in highs)


def test_fallback_handles_empty_report():
    out = build_fallback_report({}, reason="空报告")
    assert_ai_report_schema(out, expect_degraded=True)
    assert "数据不足" in out["diagnosis"]


def test_fallback_handles_partial_report(fake_report):
    del fake_report["score"]
    del fake_report["blindspots"]
    out = build_fallback_report(fake_report)
    assert_ai_report_schema(out, expect_degraded=True)
    assert out["numeric_check"]["ok"], out["numeric_check"]["mismatches"]


def test_fallback_notes_upstream_degradation(fake_report):
    fake_report["meta"]["degraded"] = True
    fake_report["meta"]["degrade_reasons"] = ["POI 配额不足，使用快照"]
    out = build_fallback_report(fake_report)
    assert "数据降级" in out["diagnosis"]
    assert "POI 配额不足，使用快照" in out["diagnosis"]


def test_fallback_no_siting_suggestions_when_empty(fake_report):
    fake_report["siting"] = []
    out = build_fallback_report(fake_report)
    assert not any("候选补点点位" in s["text"] for s in out["suggestions"])
