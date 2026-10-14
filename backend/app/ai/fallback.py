"""模板化降级报告 —— S4 交付物 5/6。

无 Key / 超时 / 校验失败时的功能保底。要求（CONTRACT 6.2 + SOLUTION 6.1）：
**无 LLM 也必须产出同一 Schema 的可用报告** —— degraded=True，内容由规则引擎填充。

关键约束：模板里出现的**每一个数字都必须来自传入的报告**，
因此本模块在返回前会调用 validation.validate_numbers 对自身做一次自检
（结果放进 numeric_check 字段），确保降级路径与 LLM 路径遵守同一条铁律。
"""
from __future__ import annotations

from typing import Any

from .knowledge import CATEGORY_NAMES, Clause, display_name, search
from .validation import (
    build_metric_digest,
    fmt_area,
    fmt_seconds,
    priority_rank,
    validate_numbers,
)

__all__ = [
    "build_fallback_report",
    "GRADE_TEXT",
    "COVERAGE_GOOD",
    "COVERAGE_FAIR",
    "COVERAGE_POOR",
    "BLIND_WALK_SECONDS",
]

#: 覆盖率评分阈值（0~1）
COVERAGE_GOOD = 0.8
COVERAGE_FAIR = 0.6
COVERAGE_POOR = 0.4

GRADE_TEXT: dict[str, str] = {"A": "优秀", "B": "良好", "C": "一般", "D": "不足"}

#: 「周边 1 公里内无该设施」对应的步行耗时阈值（SOLUTION 4.4：1000 m ÷ 1.25 m/s ≈ 750 s）
BLIND_WALK_SECONDS = 750.0

#: 时间档位按报告实际情况选取（不写死 15）
_TIER_PREFERENCE: tuple[int, ...] = (15, 10, 5)


def _data_tier(report: dict, wanted: int) -> int | None:
    """确认报告里确实存在某个分钟档位，返回该档位；否则返回 None。

    模板中的所有档位数字都必须经过本函数，避免「报告里没有 15 分钟圈却写 15 分钟」。
    """
    iso = (report or {}).get("isochrones") or {}
    keys: set[int] = set()
    for k in iso:
        try:
            keys.add(int(float(k)))
        except (TypeError, ValueError):
            continue
    if wanted in keys:
        return wanted
    for t in _TIER_PREFERENCE:
        if t in keys:
            return t
    return None


def _num(v: Any) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def _template_allowed(report: dict) -> set[float]:
    """模板自检额外放行的数值。

    两类，都是**模板自身定义或精确换算**得到的，不含任何外部事实：
    1. 阈值常量的百分号写法（如 COVERAGE_POOR=0.4 会以 "40.0%" 出现在诊断里）；
    2. 秒 -> 分钟、平方米 -> 平方公里的**精确换算值**（fmt_seconds / fmt_area 的输出）。
    """
    allowed: set[float] = {
        COVERAGE_GOOD * 100, COVERAGE_FAIR * 100, COVERAGE_POOR * 100,
        COVERAGE_GOOD, COVERAGE_FAIR, COVERAGE_POOR,
        BLIND_WALK_SECONDS / 60,   # 「1 公里 ≈ 750 秒」的分钟换算
    }
    # 模板里的档位数字（如「15 分钟步行可达范围」）来自报告 isochrones 的 key
    iso_keys = (report or {}).get("isochrones") or {}
    allowed |= {float(t) for t in _TIER_PREFERENCE if str(t) in iso_keys or t in iso_keys}
    digest = build_metric_digest(report if isinstance(report, dict) else {})
    seconds: list[float] = []
    for c in digest.get("categories") or []:
        sec = _num(c.get("nearest_walk_seconds"))
        if sec is not None:
            seconds.append(sec)
        for p in c.get("top_pois") or []:
            sec = _num(p.get("walk_seconds"))
            if sec is not None:
                seconds.append(sec)
    areas: list[float] = [float(i["area_m2"]) for i in (digest.get("isochrones") or [])
                          if _num(i.get("area_m2")) is not None]
    if _num(digest.get("blindspot_total_area_m2")) is not None:
        areas.append(float(digest["blindspot_total_area_m2"]))
    for b in digest.get("blindspots") or []:
        if _num(b.get("area_m2")) is not None:
            areas.append(float(b["area_m2"]))
    for sec in seconds:
        allowed.add(round(sec / 60, 1))
    for m2 in areas:
        allowed.add(round(m2 / 1e6, 1))
    return allowed


def _pick_clause(clauses: list[Clause], action: str) -> Clause | None:
    for c in clauses:
        if c.action == action:
            return c
    return clauses[0] if clauses else None


def _evidence(clauses: list[Clause], limit: int = 3) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for c in clauses:
        if c.source in seen:
            continue
        seen.add(c.source)
        out.append({"clause": c.text, "source": c.source})
        if len(out) >= limit:
            break
    return out


def build_fallback_report(
    report: dict,
    *,
    reason: str = "",
    clauses: list[Clause] | None = None,
) -> dict:
    """由规则引擎填充的模板报告（与 LLM 输出同 Schema）。

    :param report:  CONTRACT 4 的完整报告 dict
    :param reason:  降级原因（写入 degrade_reason，便于前端明示与排障）
    :param clauses: 预先检索的条文；为空则按报告特征现场检索
    :return: ai_report 子对象（degraded=True）
    """
    report = report if isinstance(report, dict) else {}
    digest = build_metric_digest(report)
    categories: list[dict[str, Any]] = list(digest.get("categories") or [])
    iso: list[dict[str, Any]] = list(digest.get("isochrones") or [])
    blind_spots: list[dict[str, Any]] = list(digest.get("blindspots") or [])
    meta = report.get("meta") or {}
    score_total = _num(digest.get("score_total"))
    grade = str(digest.get("score_grade") or "")

    # ---- 检索条文（模板也必须引用真实条文）----
    if not clauses:
        missing_feats = [str(c.get("key")) for c in categories if (_num(c.get("count")) or 0) <= 0]
        clauses = search(
            features=missing_feats or ["_overall"],
            query=" ".join(str(c.get("name") or "") for c in categories),
            k=6,
        )
    clause_list = list(clauses)

    def cname(c: dict[str, Any]) -> str:
        return str(c.get("name") or display_name(str(c.get("key"))))

    # ---- 诊断（规则引擎逐句生成，句句可追溯到指标）----
    sentences: list[str] = []
    addr = digest.get("address")
    prefix = f"{addr} " if addr else ""
    if score_total is not None:
        head = f"{prefix}综合体检得分 {score_total:.1f} 分"
        if grade:
            head += f"，等级 {grade}（{GRADE_TEXT.get(grade, grade)}）"
        sentences.append(head + "。")
    else:
        sentences.append(f"{prefix}综合体检得分：数据不足。")

    tier = _data_tier(report, 15)
    iso15 = next(
        (i for i in iso if tier is not None and _num(i.get("minutes")) == float(tier)),
        iso[-1] if iso else None,
    )
    if iso15 and _num(iso15.get("area_m2")) is not None:
        tail = ""
        if _num(iso15.get("n_samples")) is not None:
            tail = f"，等时圈采样点数 {int(_num(iso15.get('n_samples')) or 0)} 个"
        # 档位数字来自报告（isochrones 的 key），报告缺失时不要凭空写 "15 分钟"
        head = f"{tier} 分钟步行可达范围 " if tier else "步行可达范围 "
        sentences.append(f"{head}{fmt_area(iso15.get('area_m2'))}{tail}。")

    if categories:
        good = [c for c in categories if (_num(c.get("coverage_score")) or 0) >= COVERAGE_GOOD]
        poor = [c for c in categories if (_num(c.get("coverage_score")) or 0) < COVERAGE_POOR]
        zero = [c for c in categories if (_num(c.get("count")) or 0) <= 0]
        if good:
            sentences.append(
                f"覆盖较好的设施类别为{'、'.join(cname(c) for c in good)}，"
                f"其覆盖率评分均不低于 {COVERAGE_GOOD * 100:.1f}%。"
            )
        if poor:
            sentences.append(
                f"覆盖率评分低于 {COVERAGE_POOR * 100:.1f}% 的短板类别为{'、'.join(cname(c) for c in poor)}。"
            )
        if zero:
            sentences.append(f"圈内未检索到设施的类别为{'、'.join(cname(c) for c in zero)}，需重点核查是否为真实缺口。")
        nearest = [
            (cname(c), _num(c.get("nearest_walk_seconds")))
            for c in categories
            if _num(c.get("nearest_walk_seconds")) is not None
        ]
        if nearest:
            nearest.sort(key=lambda t: t[1] or 0.0)
            sentences.append(f"步行可达性最好的是{nearest[0][0]}，最近一处约 {fmt_seconds(nearest[0][1])}。")
    else:
        sentences.append("设施覆盖明细数据不足，无法给出分类归因。")

    total_blind = _num(digest.get("blindspot_total_area_m2"))
    if total_blind is not None:
        if total_blind > 0:
            miss_names: list[str] = []
            for b in blind_spots:
                for m in (b.get("missing") or []):
                    nm = CATEGORY_NAMES.get(str(m), str(m))
                    if nm not in miss_names:
                        miss_names.append(nm)
            sentences.append(
                f"识别出服务盲区总面积 {fmt_area(total_blind)}"
                + (f"，主要缺失类别为{'、'.join(miss_names)}。" if miss_names else "。")
            )
        else:
            sentences.append("未识别出服务盲区，设施覆盖较为均衡。")

    if meta.get("degraded"):
        reasons = "、".join(str(r) for r in (digest.get("degrade_reasons") or []))
        sentences.append(f"本次分析存在数据降级{f'（{reasons}）' if reasons else ''}，结论置信度相应降低。")
    sentences.append(
        "以上结论由确定性地理计算引擎产出；本报告为规则模板生成（未启用或未完成 LLM 解读），诊断文字为指标的直接映射。"
    )

    # ---- 建议（规则引擎填充同样 Schema）----
    suggestions: list[dict[str, str]] = []
    if categories:
        zero = [c for c in categories if (_num(c.get("count")) or 0) <= 0]
        poor = sorted(
            [
                c for c in categories
                if (_num(c.get("coverage_score")) or 0) < COVERAGE_FAIR and c not in zero
            ],
            key=lambda c: _num(c.get("coverage_score")) or 0.0,
        )
        for c in zero[:3]:
            key = str(c.get("key"))
            cl = _pick_clause(clause_list, key) or _pick_clause(clause_list, "coverage")
            suggestions.append({
                "text": (
                    f"{cname(c)}：15 分钟圈内设施数量为 {int(_num(c.get('count')) or 0)} 个，"
                    "属于基本保障类缺口，建议优先补点或与相邻社区共享设施"
                    + (f"（参考：{cl.source}）。" if cl else "。")
                ),
                "priority": "high",
            })
        for c in poor[:3]:
            cov = _num(c.get("coverage_score")) or 0.0
            sec = _num(c.get("nearest_walk_seconds"))
            suggestions.append({
                "text": (
                    f"{cname(c)}：覆盖率评分 {cov:.2f}，低于 {COVERAGE_FAIR:.2f} 的达标线"
                    + (f"，最近一处步行耗时约 {fmt_seconds(sec)}" if sec is not None else "")
                    + "；建议结合现状设施复合设置，并打通步行通道断点。"
                ),
                "priority": "medium",
            })
    if blind_spots:
        worst = max(blind_spots, key=lambda b: _num(b.get("area_m2")) or 0.0)
        miss = "、".join(CATEGORY_NAMES.get(str(m), str(m)) for m in (worst.get("missing") or []))
        cl = _pick_clause(clause_list, "siting")
        suggestions.append({
            "text": (
                f"面积最大的盲区约 {fmt_area(worst.get('area_m2'))}，缺失类别为{miss or '数据不足'}；"
                "建议纳入社区更新行动计划，优先投放基本保障类设施"
                + (f"（参考：{cl.source}）。" if cl else "。")
            ),
            "priority": "high",
        })
    for s in (digest.get("siting") or [])[:2]:
        suggestions.append({
            "text": f"候选补点点位（{s.get('lat')}, {s.get('lng')}）：{s.get('reason') or '引擎推荐的补点位置'}。",
            "priority": "medium",
        })
    if not suggestions:
        suggestions.append({
            "text": "本次报告缺少可用的设施与盲区指标（数据不足），建议先补齐 POI 与算路数据后重新体检。",
            "priority": "low",
        })
    suggestions = sorted(suggestions[:6], key=lambda s: priority_rank(s["priority"]))

    out: dict[str, Any] = {
        "degraded": True,
        "diagnosis": "".join(sentences),
        "evidence": _evidence(clause_list),
        "suggestions": suggestions,
        "degrade_reason": reason or "未启用 LLM，使用规则模板生成",
        "generator": "template",
    }

    # ---- 自检：模板自身也必须通过数值一致性校验 ----
    # 说明：evidence[].clause 是知识库里**逐字复制**的条文原文（非生成内容），
    # 其中的规范数值（如服务半径 300 m）不属于报告指标，故不纳入自检范围；
    # 生成性质的 diagnosis 与 suggestions 则必须逐数字可追溯。
    chk = validate_numbers(
        {
            "diagnosis": out["diagnosis"],
            "suggestions": [s["text"] for s in out["suggestions"]],
        },
        report,
        allowed=_template_allowed(report),
    )
    out["numeric_check"] = {
        "ok": bool(chk["ok"]),
        "n_numbers": chk["n_numbers"],
        "mismatches": chk["mismatches"],
    }
    return out
