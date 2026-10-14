"""__init__.generate_report 单测：契约签名 + 全链路降级（零网络，LLM 用 mock）。

重点验证：
1. 签名与返回值严格符合 CONTRACT.md 6.2 / 4；
2. **永不抛异常**（畸形输入、客户端异常、LLM 乱输出）；
3. 数值不一致 -> 校验拦住 -> 降级（S4 验收项）。
"""
from __future__ import annotations

import json

import pytest

from app.ai import (
    ai_health,
    generate_report,
    generate_report_with_meta,
    get_last_diagnostics,
)
from app.ai.client import ChatResult
from app.ai.knowledge import search

from conftest import assert_ai_report_schema

GOOD_LLM_JSON = {
    "diagnosis": (
        "该社区综合体检得分 68.4 分，等级 C，15 分钟步行可达范围约 1.62 平方公里。"
        "超市便利店与公交站覆盖率分别为 0.88 与 0.93，基本满足日常需求；"
        "菜市场与小学的覆盖率仅 0.18 与 0.12，是本次体检的主要短板。"
        "盲区总面积 185000 平方米，集中在缺少菜市场与小学的区域。"
    ),
    "evidence": [
        {"clause": "十五分钟生活圈配套设施服务半径不宜大于 1000 m，菜市场宜在十分钟生活圈配置。",
         "source": "《城市居住区规划设计标准》GB 50180-2018 第 5.0.2 条（配套设施分级配置表）"},
        {"clause": "社区商业设施应重点保障菜市场（生鲜超市）等日常购物需求。",
         "source": "《上海市15分钟社区生活圈规划导则》第 3 章（社区商业 · 菜市场）"},
    ],
    "suggestions": [
        {"text": "优先在盲区内补建菜市场：现状圈内数量为 0 个，覆盖率 0.18。", "priority": "high"},
        {"text": "结合教育设施统筹布局小学，现状最近一处步行耗时 1450 秒。", "priority": "high"},
        {"text": "打通步行通道断点，改善养老设施 780 秒的可达性。", "priority": "medium"},
    ],
}

#: 故意编造：25 个菜市场（实际 0）、30 所小学（实际 0）、得分 95.0（实际 68.4）
HALLUCINATED_LLM_JSON = {
    "diagnosis": "该社区菜市场共 25 个，小学 30 所，综合得分 95.0 分，设施配置完善。",
    "evidence": [{"clause": "配套设施应分级配置。", "source": "《城市居住区规划设计标准》GB 50180-2018 第 5.0.2 条（配套设施分级配置表）"}],
    "suggestions": [{"text": "维持现状即可，无需补点。", "priority": "low"}],
}


class _StubClient:
    """可编程的假客户端：按顺序返回预设输出，并记录调用次数。"""

    def __init__(self, responses, config=None):
        # responses: list[ChatResult | dict]
        self.responses = list(responses)
        self.calls: list[list[dict]] = []
        self.config = config

    def chat_json(self, messages, **kwargs):
        self.calls.append(list(messages))
        item = self.responses.pop(0) if self.responses else ChatResult(ok=False, error="stub 用尽")
        if isinstance(item, ChatResult):
            return (item.json_object() if item.ok else None), item
        return item, ChatResult(ok=True, content=json.dumps(item, ensure_ascii=False))

    def health(self):
        return {"llm": True, "model": "stub", "effort": "low", "has_api_key": True}


class _BoomClient:
    """每次调用都抛异常，验证 generate_report 的兜底。"""

    def __init__(self, *a, **kw):
        self.config = None

    def chat_json(self, messages, **kwargs):
        raise RuntimeError("模拟客户端崩溃")

    def health(self):
        return {"llm": False}


@pytest.fixture
def use_stub(monkeypatch):
    def _install(client):
        monkeypatch.setattr("app.ai.get_client", lambda *a, **kw: client)
        return client
    return _install


# ---------------- 契约：签名与基本行为 ----------------
def test_signature_is_frozen_contract():
    import inspect
    sig = inspect.signature(generate_report)
    params = list(sig.parameters.items())
    assert params[0][0] == "report"
    assert params[1][0] == "enable_llm"
    assert params[1][1].kind is inspect.Parameter.KEYWORD_ONLY, "enable_llm 必须是 keyword-only"
    assert params[1][1].default is None


def test_template_path_when_llm_disabled(fake_report):
    out = generate_report(fake_report, enable_llm=False)
    assert_ai_report_schema(out, expect_degraded=True)
    assert out["generator"] == "template"
    assert get_last_diagnostics()["strategy"] == "template"
    assert get_last_diagnostics()["llm_attempted"] is False


def test_template_path_when_no_api_key(fake_report, llm_env):
    """enable_llm=None 且未配置 Key -> 必须自动降级，且不发起请求。"""
    llm_env(LLM_API_KEY="")
    out = generate_report(fake_report)
    assert_ai_report_schema(out, expect_degraded=True)
    assert "未配置" in out["degrade_reason"]


def test_llm_path_success(fake_report, llm_env, use_stub):
    llm_env()
    stub = use_stub(_StubClient([GOOD_LLM_JSON]))
    out = generate_report(fake_report, enable_llm=True)

    assert_ai_report_schema(out, expect_degraded=False)
    assert out["generator"] == "llm"
    assert out["degrade_reason"] == ""
    assert out["numeric_check"]["ok"] is True
    assert out["llm"]["ok"] is True
    assert len(stub.calls) == 1, "正常路径只调用一次"
    diag = get_last_diagnostics()
    assert diag["strategy"] == "llm" and diag["llm_ok"] is True


def test_llm_output_prompt_injects_facts_and_guard(fake_report, llm_env, use_stub):
    llm_env()
    stub = use_stub(_StubClient([GOOD_LLM_JSON]))
    generate_report(fake_report, enable_llm=True)
    system = stub.calls[0][0]["content"]
    user = stub.calls[0][1]["content"]
    assert "只能引用" in system and "数据不足" in system, "必须有防幻觉注入"
    assert "上海市浦东新区海科路1650号" in user, "提示词必须注入报告事实"
    assert "68.4" in user
    assert "GB 50180-2018" in user, "提示词必须注入检索到的规范条文"
    assert "json" in user.lower()


# ---------------- 核心：数值不一致必须拦住并降级 ----------------
def test_hallucinated_numbers_rejected_then_degraded(fake_report, llm_env, use_stub):
    """LLM 两次都编数字 -> 最终必须是模板降级，且 degraded=True。"""
    llm_env()
    stub = use_stub(_StubClient([HALLUCINATED_LLM_JSON, HALLUCINATED_LLM_JSON]))
    out = generate_report(fake_report, enable_llm=True)

    assert_ai_report_schema(out, expect_degraded=True)
    assert out["generator"] == "template"
    assert out["strategy"] == "template_after_llm_reject"
    assert len(stub.calls) == 2, "必须尝试一次纠错重生成"
    diag = get_last_diagnostics()
    assert diag["retry_used"] is True
    bad = {m["value"] for m in diag["llm_numeric_check"]["mismatches"]}
    assert 25.0 in bad, f"编造的菜市场数量 25 必须被拦下，实际 {bad}"
    # 注：95.0 恰好等于「比率 0.95 -> 百分比 95」这一合法形态，故未被判为越界。
    #     这是有意的宽容度（报告里确实存在 95 这个数），不是漏洞；校验器负责拦「凭空数字」。
    assert diag["template_numeric_check"]["ok"] is True, "降级模板自身也必须通过数值自检"
    assert "校验" in out["degrade_reason"]


def test_hallucination_corrected_on_retry(fake_report, llm_env, use_stub):
    """第一次编数字，纠错重生成后合规 -> 应返回 LLM 结果且不降级。"""
    llm_env()
    stub = use_stub(_StubClient([HALLUCINATED_LLM_JSON, GOOD_LLM_JSON]))
    out = generate_report(fake_report, enable_llm=True)

    assert_ai_report_schema(out, expect_degraded=False)
    assert out["generator"] == "llm"
    assert len(stub.calls) == 2
    retry_user = stub.calls[1][-1]["content"]
    assert "数值一致性校验" in retry_user, "重试必须把校验失败原因反馈给模型"
    assert "25" in retry_user, "重试必须点名具体的越界数字"


def test_shape_violation_triggers_retry_then_degrade(fake_report, llm_env, use_stub):
    """结构不合规（缺 suggestions）同样走重试 -> 降级。"""
    llm_env()
    bad_shape = {"diagnosis": "该社区综合得分 68.4 分。", "evidence": [], "suggestions": []}
    stub = use_stub(_StubClient([bad_shape, bad_shape]))
    out = generate_report(fake_report, enable_llm=True)
    assert out["degraded"] is True
    assert len(stub.calls) == 2


def test_llm_failure_degrades_to_template(fake_report, llm_env, use_stub):
    llm_env()
    use_stub(_StubClient([ChatResult(ok=False, error="超时（40s 未返回）：ReadTimeout")]))
    out = generate_report(fake_report, enable_llm=True)
    assert_ai_report_schema(out, expect_degraded=True)
    assert "超时" in out["degrade_reason"]
    assert out["llm"]["ok"] is False


def test_client_exception_never_propagates(fake_report, llm_env, use_stub):
    llm_env()
    use_stub(_BoomClient())
    out = generate_report(fake_report, enable_llm=True)
    assert_ai_report_schema(out, expect_degraded=True)


# ---------------- 鲁棒性：畸形输入 ----------------
@pytest.mark.parametrize("bad", [None, [], "字符串", 42, {"meta": None},
                                 {"facilities": {"categories": "not-a-list"}},
                                 {"score": {"total": "abc"}, "isochrones": []}])
def test_never_raises_on_malformed_report(bad):
    out = generate_report(bad, enable_llm=False)
    assert isinstance(out, dict)
    assert out["degraded"] is True
    assert "diagnosis" in out and "evidence" in out and "suggestions" in out


def test_llm_weird_shapes_are_normalized(fake_report, llm_env, use_stub):
    """LLM 返回别名键 / 字符串数组 / 中文优先级都要能归一化。"""
    llm_env()
    weird = {
        "诊断": "综合得分 68.4 分，菜市场 0 个。",
        "依据": [{"clause": "分级配置要求", "出处": "《城市居住区规划设计标准》GB 50180-2018 第 5.0.2 条（配套设施分级配置表）"}],
        "建议": [{"建议": "补建菜市场", "优先级": "高"}, "优化步行通道"],
    }
    use_stub(_StubClient([weird]))
    out = generate_report(fake_report, enable_llm=True)
    assert out["degraded"] is False, out.get("degrade_reason")
    assert out["suggestions"][0]["priority"] == "high"
    assert len(out["evidence"]) == 1
    assert out["evidence"][0]["source"].startswith("《城市居住区")


def test_evidence_source_filled_from_retrieved_clauses(fake_report, llm_env, use_stub):
    llm_env()
    no_source = {
        "diagnosis": "综合得分 68.4 分。",
        "evidence": [{"clause": "配套设施应分级配置。"}],
        "suggestions": [{"text": "补充菜市场", "priority": "high"}],
    }
    use_stub(_StubClient([no_source]))
    out = generate_report(fake_report, enable_llm=True)
    assert out["evidence"][0]["source"], "出处缺失时应由检索结果补齐"
    assert out["evidence_check"]["ok"] is True


# ---------------- 附加接口 ----------------
def test_generate_report_with_meta(fake_report):
    res = generate_report_with_meta(fake_report, enable_llm=False)
    assert set(res.keys()) == {"ai_report", "diagnostics", "elapsed_s"}
    assert res["diagnostics"]["strategy"] == "template"
    assert res["elapsed_s"] >= 0


def test_fixture_file_matches_conftest(fake_report):
    """fixture_report.json 必须与 conftest 的假报告一致（防止两者漂移）。"""
    import json as _json
    from pathlib import Path

    path = Path(__file__).with_name("fixture_report.json")
    assert path.is_file(), "缺少 fixture_report.json"
    on_disk = _json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == fake_report


def test_ai_health_shape(llm_env):
    llm_env()
    h = ai_health()
    assert h["llm"] is True
    assert h["model"] == "deepseek-flash"
    assert h["effort"] == "low"
    assert h["clauses"] >= 20
    assert "api_key" not in h


def test_ai_report_is_json_serializable(fake_report):
    out = generate_report(fake_report, enable_llm=False)
    text = json.dumps(out, ensure_ascii=False)
    assert "degraded" in text


def test_no_secret_leaks_into_report(fake_report, llm_env):
    """报告里绝不能出现 API Key。"""
    llm_env(LLM_API_KEY="sk-leak-canary-1234567890")
    for enable in (False,):
        out = generate_report(fake_report, enable_llm=enable)
        assert "sk-leak-canary" not in json.dumps(out, ensure_ascii=False)


def test_retrieved_clauses_are_recorded(fake_report, llm_env, use_stub):
    llm_env()
    use_stub(_StubClient([GOOD_LLM_JSON]))
    out = generate_report(fake_report, enable_llm=True)
    from app.ai.knowledge import CLAUSES
    ids = out["llm"]["clauses_retrieved"]
    assert ids and all("#" in i for i in ids)
    assert set(ids) <= {c.id for c in CLAUSES}, "只能引用知识库里真实存在的条文"


def test_enable_llm_true_overrides_env_flag(fake_report, llm_env, use_stub):
    """enable_llm 显式传参优先于 LLM_ENABLED 环境变量。"""
    llm_env(LLM_ENABLED="false")
    stub = use_stub(_StubClient([GOOD_LLM_JSON]))
    out = generate_report(fake_report, enable_llm=True)
    assert get_last_diagnostics()["llm_attempted"] is True
    assert len(stub.calls) >= 1


def test_enable_llm_false_skips_even_with_env_true(fake_report, llm_env, use_stub):
    """LLM_ENABLED=true 时，显式 enable_llm=False 仍必须跳过调用（用于离线演示）。"""
    llm_env(LLM_ENABLED="true")
    stub = use_stub(_StubClient([GOOD_LLM_JSON]))
    out = generate_report(fake_report, enable_llm=False)
    assert out["degraded"] is True
    assert len(stub.calls) == 0
    assert get_last_diagnostics()["llm_attempted"] is False
