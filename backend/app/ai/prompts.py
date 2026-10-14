"""RAG 提示词构建：强制 JSON Schema 输出 + 防幻觉注入。

设计要点
--------
1. **数字主权**：提示词里注入的事实全部来自 generate_report 传入的报告，
   并明确告知「不得引入任何未给出的数字」；
2. **引用可核对**：只允许引用注入的规范条文，source 必须原样复制；
3. **结构化输出**：response_format={"type":"json_object"} + 明确 Schema，
   配合 validation 层的数值一致性校验构成双重保险；
4. **可重试**：校验失败时把 mismatches 反馈回去做一次纠错重生成。
"""
from __future__ import annotations

import json
from typing import Any, Sequence

from .knowledge import Clause, format_clauses
from .validation import build_metric_digest

__all__ = [
    "AI_REPORT_SCHEMA",
    "PRIORITIES",
    "build_messages",
    "build_user_prompt",
    "build_retry_message",
    "schema_hint",
]

#: 输出 Schema（与 CONTRACT.md 4 的 ai_report 子对象严格对齐）
AI_REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["diagnosis", "evidence", "suggestions"],
    "additionalProperties": False,
    "properties": {
        "diagnosis": {
            "type": "string",
            "description": "社区体检诊断结论，3~6 句，必须给出等时圈面积、设施数量/覆盖率等具体指标",
        },
        "evidence": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["clause", "source"],
                "additionalProperties": False,
                "properties": {
                    "clause": {
                        "type": "string",
                        "description": "规范条文要点（改写自给定条文，不得引入新数字）",
                    },
                    "source": {
                        "type": "string",
                        "description": "出处，必须原样复制给定条文的「引用格式」",
                    },
                },
            },
        },
        "suggestions": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["text", "priority"],
                "additionalProperties": False,
                "properties": {
                    "text": {"type": "string", "description": "可执行的改善建议，可引用报告指标"},
                    "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
        },
    },
}

PRIORITIES: tuple[str, ...] = ("high", "medium", "low")

_SYSTEM_PROMPT = """你是「15 分钟社区生活圈」体检报告的解读助手（RAG 报告生成器）。

【最高优先级铁律 · 违反即失败】
1. 所有数字都已由确定性地理计算引擎产出。你**只能引用用户消息中给出的数字**，
   严禁自行推算、估算、四舍五入到与给定值不同的数值，严禁引入任何未给出的数字。
   **抄写数字时必须逐字符照抄**（含小数位），例如给定的 70.75 不能写成 70 或 71，
   给定的 46875.0 不能写成 46875 或 4.7 万；宁可少写一个数字，也不要写错一个数字。
2. 若某项指标缺失或为 null，必须原样写「数据不足」，**不得猜测**。
3. 只能引用用户消息「可引用的规范条文」中列出的条文；
   evidence[].source 必须**原样复制**该条文的「引用格式」，不得改写、不得新增条文。
4. 不得编造设施名称、地址、人口数据、政策文号或年份。
5. 若报告中 meta.degraded 为 true，必须在 diagnosis 中明确说明数据存在降级及其原因。

【写作要求】
- diagnosis：3~5 句中文，**总长控制在 220 字以内**（输出 token 预算有限，
  超长会导致 JSON 被截断、整份报告判失败）。先给结论（综合评分与等级、15 分钟圈面积），
  再归因（最关键的 2~3 类短板、盲区分布特征），最后一句总体判断。
  **不要逐项罗列全部 8 类设施的数值**，只挑最能说明问题的指标。语气客观，避免营销腔。
- evidence：2~3 条，每条**不超过 80 字**，在 clause 中说明该条文与本社区现状的关系。
- suggestions：3~5 条，按 priority 排序（high 在前），每条**不超过 90 字**。每条建议必须：
  ① 指向具体设施类别或具体点位；② 尽量引用报告中的指标作为依据；
  ③ 是规划/治理层面可落地的动作（补点、共享、复合设置、优化步行通道等）。

【输出格式】
只输出一个 JSON 对象，不要 markdown 代码块、不要任何解释性前后缀。结构：
{"diagnosis": "...", "evidence": [{"clause": "...", "source": "..."}],
 "suggestions": [{"text": "...", "priority": "high"}]}"""


def schema_hint() -> str:
    """给模型看的紧凑 Schema 提示（比整份 JSON Schema 更省 token）。"""
    return json.dumps(AI_REPORT_SCHEMA, ensure_ascii=False, separators=(",", ":"))


def build_user_prompt(
    report: dict,
    clauses: Sequence[Clause],
    *,
    extra_instruction: str = "",
) -> str:
    """构建用户消息：规范条文（RAG 上下文）+ 指标事实 + 输出要求。"""
    digest = build_metric_digest(report)
    facts = json.dumps(digest, ensure_ascii=False, indent=1)

    parts = [
        "## 一、可引用的规范条文（仅可使用以下条文）",
        format_clauses(clauses),
        "",
        "## 二、本社区体检指标（唯一可用的事实与数字来源）",
        "json",
        facts,
        "",
        "## 三、输出要求",
        "严格按以下 JSON Schema 输出单个 JSON 对象：",
        schema_hint(),
        "",
        "再次强调：diagnosis / evidence / suggestions 中出现的一切数字，"
        "必须能在上面「本社区体检指标」或「规范条文」中找到原值；"
        "无法找到依据的数字一律不要写，缺失指标写「数据不足」。",
    ]
    if extra_instruction:
        parts += ["", "## 四、本次生成的额外要求", extra_instruction]
    return "\n".join(parts)


def build_messages(
    report: dict,
    clauses: Sequence[Clause],
    *,
    extra_instruction: str = "",
) -> list[dict[str, str]]:
    """构建 OpenAI 兼容的 messages。"""
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_user_prompt(report, clauses, extra_instruction=extra_instruction),
        },
    ]


def build_retry_message(mismatch_reason: str, mismatches: Sequence[dict]) -> str:
    """把校验失败的具体数字反馈给模型，用于一次纠错重生成。"""
    items = "、".join(f"「{m.get('text', m.get('value'))}」" for m in list(mismatches)[:8])
    return (
        "上一次输出未通过数值一致性校验，已被判定为幻觉。"
        f"校验结论：{mismatch_reason}。"
        + (f" 其中无法在报告中找到依据的数字为：{items}。" if items else "")
        + " 请重新生成：删除或改写所有无依据的数字，只保留「本社区体检指标」中出现的原值；"
        "缺失的指标一律写「数据不足」。仍然只输出同一个 JSON Schema。"
    )
