"""AI 层对外唯一入口 —— S4 交付物 6/6。

契约（CONTRACT.md 6.2，签名冻结）::

    def generate_report(report: dict, *, enable_llm: bool | None = None) -> dict:
        '''输入 4 的完整报告 dict，返回 4 的 ai_report 子对象。
        永不抛异常；任何失败都返回模板降级结果（degraded=True）。'''

返回值（ai_report 子对象）::

    {
      "degraded": bool,                       # 必填，true = 用了模板降级
      "diagnosis": str,
      "evidence": [{"clause": str, "source": str}],
      "suggestions": [{"text": str, "priority": "high"|"medium"|"low"}],
      # 以下为附加诊断字段（给前端/文档用，不影响契约字段）
      "degrade_reason": str,
      "generator": "llm" | "template",
      "numeric_check": {...},
      "llm": {...}                            # 脱敏后的调用元信息，绝不含 API Key
    }

执行流程::

    报告 dict
      -> 检索规范条文（knowledge.search，按缺口类别定向）
      -> 构建 RAG 提示词（prompts，强制 JSON Schema + 防幻觉注入）
      -> DeepSeek 调用（client，推理模型，effort=low，超时不外抛）
      -> 结构化输出归一化 -> 数值一致性校验（validation）
           失败 -> 带 mismatch 反馈重生成一次 -> 仍失败 -> 模板降级
      -> 模板降级（fallback，同一 Schema，degraded=True）

任何环节异常都被捕获，**永不抛出**。
"""
from __future__ import annotations

import time
from typing import Any, TypedDict

from .client import (
    ChatResult,
    DeepSeekClient,
    LLMConfig,
    VALID_EFFORTS,
    chat_json,
    get_client,
    load_dotenv,
)
from .fallback import build_fallback_report
from .knowledge import (
    CATEGORY_NAMES,
    CLAUSES,
    FEATURE_KEYS,
    Clause,
    citation_set,
    display_name,
    format_clauses,
    get_clause,
    search,
)
from .prompts import AI_REPORT_SCHEMA, PRIORITIES, build_messages, build_retry_message, schema_hint
from .validation import (
    build_metric_digest,
    build_number_set,
    extract_numbers,
    validate_evidence,
    validate_numbers,
)

__all__ = [
    "generate_report",
    "generate_report_with_meta",
    "ai_health",
    "get_last_diagnostics",
    "AIReport",
    # 子模块便捷再导出（便于 API 层与测试使用）
    "LLMConfig",
    "DeepSeekClient",
    "ChatResult",
    "chat_json",
    "get_client",
    "load_dotenv",
    "search",
    "format_clauses",
    "citation_set",
    "CLAUSES",
    "FEATURE_KEYS",
    "CATEGORY_NAMES",
    "display_name",
    "get_clause",
    "build_fallback_report",
    "build_metric_digest",
    "build_number_set",
    "extract_numbers",
    "validate_numbers",
    "validate_evidence",
    "build_messages",
    "build_retry_message",
    "schema_hint",
    "AI_REPORT_SCHEMA",
    "PRIORITIES",
]

__version__ = "1.0.0"


class AIReport(TypedDict, total=False):
    """ai_report 子对象的类型说明。"""

    degraded: bool
    diagnosis: str
    evidence: list[dict[str, str]]
    suggestions: list[dict[str, str]]
    degrade_reason: str
    generator: str
    numeric_check: dict[str, Any]
    llm: dict[str, Any]


#: 最近一次 generate_report 的可观测信息（供脚本自测与 API 层排障，不含敏感信息）
_LAST_DIAGNOSTICS: dict[str, Any] = {}

_DEFAULT_MIN_EVIDENCE = 1
_DEFAULT_MIN_SUGGESTIONS = 1


def _now() -> float:
    return time.monotonic()


def get_last_diagnostics() -> dict[str, Any]:
    """返回最近一次 generate_report 的诊断信息（浅拷贝）。"""
    return dict(_LAST_DIAGNOSTICS)


def ai_health() -> dict[str, Any]:
    """AI 层健康状态（供 GET /api/health 的 llm 字段使用）。不发起网络请求。"""
    try:
        cfg = LLMConfig.from_env()
    except Exception as exc:  # noqa: BLE001
        return {"llm": False, "model": None, "effort": None, "error": f"{type(exc).__name__}: {exc}"}
    health = DeepSeekClient(cfg).health()
    health["clauses"] = len(CLAUSES)
    return health


# --------------------------------------------------------------------------
# 输出归一化
# --------------------------------------------------------------------------
def _as_text(v: Any) -> str:
    if isinstance(v, str):
        return v.strip()
    if v is None:
        return ""
    if isinstance(v, (int, float, bool)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return " ".join(_as_text(x) for x in v).strip()
    if isinstance(v, dict):
        return _as_text(v.get("text") or v.get("clause") or v.get("content") or "")
    return str(v).strip()


def _norm_priority(v: Any) -> str:
    s = _as_text(v).lower()
    if s in PRIORITIES:
        return s
    alias = {"高": "high", "中": "medium", "低": "low", "h": "high", "m": "medium", "l": "low",
             "1": "high", "2": "medium", "3": "low"}
    return alias.get(s, "medium")


def _norm_evidence(raw: Any, clauses: list[Clause]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return items
    for item in raw:
        if isinstance(item, str):
            items.append({"clause": item, "source": ""})
            continue
        if not isinstance(item, dict):
            continue
        clause = _as_text(item.get("clause") or item.get("text") or item.get("依据"))
        source = _as_text(item.get("source") or item.get("出处") or item.get("cite"))
        if clause or source:
            items.append({"clause": clause, "source": source})
    # 出处缺失时，用检索到的条文顺序补齐（保持可追溯，不新增外部出处）
    if clauses:
        for i, it in enumerate(items):
            if not it["source"] and i < len(clauses):
                it["source"] = clauses[i].source
    return items


def _norm_suggestions(raw: Any) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return items
    for item in raw:
        if isinstance(item, str):
            items.append({"text": item, "priority": "medium"})
            continue
        if not isinstance(item, dict):
            continue
        text = _as_text(item.get("text") or item.get("suggestion") or item.get("建议") or item)
        if not text:
            continue
        items.append({"text": text, "priority": _norm_priority(item.get("priority") or item.get("优先级"))})
    return items


def _normalize(raw: dict, clauses: list[Clause]) -> dict[str, Any]:
    """把 LLM 输出归一化成契约 Schema。"""
    return {
        "diagnosis": _as_text(raw.get("diagnosis") or raw.get("诊断") or raw.get("conclusion")),
        "evidence": _norm_evidence(raw.get("evidence") or raw.get("依据") or raw.get("clauses"), clauses),
        "suggestions": _norm_suggestions(raw.get("suggestions") or raw.get("建议") or raw.get("advice")),
    }


def _shape_ok(norm: dict[str, Any]) -> tuple[bool, str]:
    """结构校验：Schema 必填项是否齐备。"""
    problems: list[str] = []
    if not norm.get("diagnosis"):
        problems.append("diagnosis 为空")
    if len(norm.get("evidence") or []) < _DEFAULT_MIN_EVIDENCE:
        problems.append("evidence 为空")
    if len(norm.get("suggestions") or []) < _DEFAULT_MIN_SUGGESTIONS:
        problems.append("suggestions 为空")
    return (not problems), "；".join(problems)


def _select_features(report: dict) -> list[str]:
    """按报告里的缺口/短板挑选检索特征（定向 RAG，而非全量塞条文）。"""
    feats: list[str] = []
    fac = (report.get("facilities") or {}) if isinstance(report, dict) else {}
    cats = [c for c in (fac.get("categories") or []) if isinstance(c, dict)]
    for c in cats:
        key = str(c.get("key") or "")
        if not key:
            continue
        count = c.get("count")
        cov = c.get("coverage_score")
        weak = (isinstance(count, (int, float)) and count <= 0) or (
            isinstance(cov, (int, float)) and cov < 0.6
        )
        if weak:
            feats.append(key)
    if isinstance(report, dict) and (report.get("blindspots") or {}).get("polygons"):
        feats.extend(["_blindspot", "_overall"])
    if isinstance(report, dict) and report.get("siting"):
        feats.append("_siting")
    return feats or ["_overall"]


def _search_clauses(report: dict, k: int = 7) -> list[Clause]:
    """按报告缺口检索条文；检索失败也不抛异常。"""
    try:
        return list(search(features=_select_features(report), query="", k=k))
    except Exception:  # noqa: BLE001
        return list(CLAUSES[:k])


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def generate_report(report: dict, *, enable_llm: bool | None = None) -> dict:
    """输入 CONTRACT.md 4 的完整报告 dict，返回 4 的 ai_report 子对象。

    永不抛异常；任何失败都返回模板降级结果（degraded=True）。
    """
    try:
        return _generate(report, enable_llm=enable_llm)
    except Exception as exc:  # noqa: BLE001 - 契约要求：绝不外抛
        # 极端兜底：连模板都失败时，返回最小可用结构，保证 API 不 500
        try:
            minimal = build_fallback_report(report if isinstance(report, dict) else {},
                                            reason=f"AI 层异常兜底：{type(exc).__name__}: {exc}")
            minimal["degraded"] = True
            return minimal
        except Exception:  # noqa: BLE001
            return {
                "degraded": True,
                "diagnosis": "数据不足：AI 解读与模板降级均未能完成。",
                "evidence": [],
                "suggestions": [{"text": "请检查报告 JSON 是否符合契约后重试。", "priority": "low"}],
                "degrade_reason": f"AI 层异常：{type(exc).__name__}: {exc}",
                "generator": "template",
            }


def generate_report_with_meta(
    report: dict,
    *,
    enable_llm: bool | None = None,
) -> dict[str, Any]:
    """与 generate_report 相同，但额外返回执行诊断（便于自测脚本与排障）。"""
    started = _now()
    out = generate_report(report, enable_llm=enable_llm)
    return {
        "ai_report": out,
        "diagnostics": dict(_LAST_DIAGNOSTICS),
        "elapsed_s": round(_now() - started, 3),
    }


def _generate(report: dict, *, enable_llm: bool | None) -> dict:
    started = _now()
    diag: dict[str, Any] = {
        "version": __version__,
        "llm_attempted": False,
        "llm_ok": False,
        "strategy": "template",
        "reason": "",
        "retry_used": False,
        "numeric_check": None,
        "evidence_check": None,
        "attempts": [],
    }
    _LAST_DIAGNOSTICS.clear()
    _LAST_DIAGNOSTICS.update(diag)

    safe_report = report if isinstance(report, dict) else {}
    if not isinstance(report, dict):
        diag["reason"] = f"输入不是 dict（{type(report).__name__}），直接模板降级"

    clauses = _search_clauses(safe_report)

    # ---- 是否启用 LLM ----
    cfg = LLMConfig.from_env()
    want_llm = cfg.enabled if enable_llm is None else bool(enable_llm)
    if not want_llm:
        diag["reason"] = "LLM 未启用（enable_llm=False 或 LLM_ENABLED=false），使用规则模板"
        return _finish_template(safe_report, clauses, diag, started)
    if not cfg.configured:
        diag["reason"] = "LLM 未配置（缺少 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL），使用规则模板"
        return _finish_template(safe_report, clauses, diag, started)

    diag["llm_attempted"] = True
    diag["config"] = cfg.safe_dict()
    diag["clauses"] = [c.id for c in clauses]

    client = get_client(cfg)
    messages = build_messages(safe_report, clauses)

    # ---- 第 1 次调用 ----
    raw, res = client.chat_json(messages)
    diag["attempts"].append(res.as_dict())

    if not res.ok or raw is None:
        diag["reason"] = f"LLM 调用失败：{res.error or '未知错误'}"
        return _finish_template(safe_report, clauses, diag, started, llm_meta=res)

    norm = _normalize(raw, clauses)
    shape_ok, shape_reason = _shape_ok(norm)
    clause_texts = [c.text for c in clauses]
    num = validate_numbers(
        {
            "diagnosis": norm["diagnosis"],
            "evidence": [e["clause"] for e in norm["evidence"]],
            "suggestions": [s["text"] for s in norm["suggestions"]],
        },
        safe_report,
        clauses=clause_texts,
    )
    diag["numeric_check"] = {"ok": num["ok"], "n_numbers": num["n_numbers"],
                             "mismatches": num["mismatches"][:8], "reason": num["reason"]}
    diag["llm_numeric_check"] = dict(diag["numeric_check"])   # 保留「被拦下的越界数字」供排障
    ev = validate_evidence(norm["evidence"], clauses)
    diag["evidence_check"] = ev

    # ---- 校验失败 -> 一次纠错重生成 ----
    if not shape_ok or not num["ok"]:
        why = shape_reason if not shape_ok else num["reason"]
        diag["first_reject_reason"] = why
        diag["reason"] = f"首次输出未通过校验（{why}），尝试纠错重生成"
        retry_messages = list(messages) + [
            {"role": "assistant", "content": res.content[:2000]},
            {"role": "user", "content": build_retry_message(why, num["mismatches"])},
        ]
        raw2, res2 = client.chat_json(retry_messages)
        diag["retry_used"] = True
        diag["attempts"].append(res2.as_dict())

        if res2.ok and raw2 is not None:
            norm2 = _normalize(raw2, clauses)
            shape_ok2, shape_reason2 = _shape_ok(norm2)
            num2 = validate_numbers(
                {
                    "diagnosis": norm2["diagnosis"],
                    "evidence": [e["clause"] for e in norm2["evidence"]],
                    "suggestions": [s["text"] for s in norm2["suggestions"]],
                },
                safe_report,
                clauses=clause_texts,
            )
            diag["numeric_check"] = {"ok": num2["ok"], "n_numbers": num2["n_numbers"],
                                     "mismatches": num2["mismatches"][:8], "reason": num2["reason"]}
            diag["llm_numeric_check"] = dict(diag["numeric_check"])
            if shape_ok2 and num2["ok"]:
                return _finish_llm(norm2, clauses, diag, started, res2,
                                   validate_evidence(norm2["evidence"], clauses))
            why2 = shape_reason2 if not shape_ok2 else num2["reason"]
            diag["reason"] = f"纠错重生成仍未通过校验（{why2}），降级为规则模板"
        else:
            diag["reason"] = f"纠错重生成调用失败（{res2.error or '未知错误'}），降级为规则模板"
        diag["first_call_rejected"] = True
        out = _finish_template(safe_report, clauses, diag, started, llm_meta=res2)
        diag["strategy"] = "template_after_llm_reject"
        out["strategy"] = "template_after_llm_reject"
        _LAST_DIAGNOSTICS.clear()
        _LAST_DIAGNOSTICS.update(diag)
        return out

    return _finish_llm(norm, clauses, diag, started, res, ev)


def _finish_llm(
    norm: dict[str, Any],
    clauses: list[Clause],
    diag: dict[str, Any],
    started: float,
    res: ChatResult,
    evidence_check: dict[str, Any],
) -> dict:
    """LLM 路径的成功收尾。"""
    diag["strategy"] = "llm"
    diag["llm_ok"] = True
    out: dict[str, Any] = {
        "degraded": False,
        "diagnosis": norm["diagnosis"],
        "evidence": norm["evidence"],
        "suggestions": norm["suggestions"],
        "degrade_reason": "",
        "generator": "llm",
        "numeric_check": diag.get("numeric_check"),
        "evidence_check": evidence_check,
        "llm": {
            **res.as_dict(),
            "effort": (diag.get("config") or {}).get("effort"),
            "clauses_retrieved": [c.id for c in clauses],
        },
    }
    diag["elapsed_s"] = round(_now() - started, 3)
    diag["reason"] = (
        "LLM 纠错重生成后通过数值一致性校验" if diag.get("retry_used")
        else "LLM 生成通过数值一致性校验"
    )
    _LAST_DIAGNOSTICS.clear()
    _LAST_DIAGNOSTICS.update(diag)
    return out


def _finish_template(
    report: dict,
    clauses: list[Clause],
    diag: dict[str, Any],
    started: float,
    llm_meta: ChatResult | None = None,
) -> dict:
    """降级路径的收尾：模板报告 + 诊断信息。"""
    out = build_fallback_report(report, reason=diag.get("reason", ""), clauses=clauses)
    diag["strategy"] = "template"
    diag["llm_ok"] = False
    diag["template_numeric_check"] = {
        "ok": (out.get("numeric_check") or {}).get("ok"),
        "n_numbers": (out.get("numeric_check") or {}).get("n_numbers"),
        "mismatches": (out.get("numeric_check") or {}).get("mismatches", [])[:8],
    }
    if diag.get("llm_attempted"):
        # 有 LLM 参与时，numeric_check 保留 LLM 输出的校验结论（越界数字最有排障价值）；
        # 纯模板路径则记录模板自检结论。
        diag.setdefault("numeric_check", {})
        if not diag.get("llm_numeric_check"):
            diag["numeric_check"] = {
                "ok": (out.get("numeric_check") or {}).get("ok"),
                "n_numbers": (out.get("numeric_check") or {}).get("n_numbers"),
                "mismatches": [],
                "reason": "模板自检",
            }
    if llm_meta is not None:
        out["llm"] = llm_meta.as_dict()
    diag["elapsed_s"] = round(_now() - started, 3)
    _LAST_DIAGNOSTICS.clear()
    _LAST_DIAGNOSTICS.update(diag)
    return out
