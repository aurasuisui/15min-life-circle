"""数值一致性校验：LLM 只能说报告里已有的数字。

铁律：**所有数字由确定性引擎产出，LLM 只做解释、归因与表达。**
本模块提供三道闸门：

1. extract_numbers  —— 从 LLM 输出（及 JSON 文本）中抽出所有「指标性数字」，
   自动剔除条款编号（GB 50180-2018 / 第 5.0.2 条）与出处标题里的数字；
2. \build_number_set —— 从报告 JSON 递归收集全部合法数值，
   并补充「百分比形态」与「清洗漏斗比值派生值」，容忍 0.82 / 82 / 82% 等价写法；
3. \validate_numbers —— 比对，给出 ok / mismatches / hit_ratio / reason。

另附 \build_metric_digest：把报告压成一份**只含事实数字**的紧凑指标摘要，
供提示词注入与降级模板共用（保证两条路径看到的事实完全一致）。
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

__all__ = [
    "extract_numbers",
    "build_number_set",
    "validate_numbers",
    "validate_evidence",
    "build_metric_digest",
    "strip_clause_ids",
    "strip_dates",
    "fmt_seconds",
    "fmt_area",
    "priority_rank",
]

# --------------------------------------------------------------------------
# 预处理：剔除规范编号 / 条号里的数字，避免误判
# --------------------------------------------------------------------------
_SOURCE_TITLE = "《[^》]{0,40}》"
_CLAUSE_ID_RE = re.compile(
    r"GB\s*/?\s*T?\s*\d{4,5}(?:\s*[-—]\s*\d{2,4})?"            # GB 50180-2018
    r"|第\s*\d+(?:\.\d+)*\s*条"                                  # 第 5.0.2 条
    r"|\d+(?:\.\d+){2,}"                                        # 3.0.4 式条号（>=3 段）
    r"|\d+\.\d+\s*(?=条)"                                       # 5.0 条
    r"|\d{4}\s*年(?:版|版次|修订)"                                   # 2018 年版
)
_CJK_NUM = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4,
            "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_CJK_UNITS = ("个", "处", "所", "家", "座", "条", "分钟", "米", "公里", "千米",
              "平方米", "公顷", "平方公里", "秒", "类", "项", "倍", "人", "岁")
_CJK_NUM_RE = re.compile(r"([零一二两三四五六七八九十])\s*(" + "|".join(_CJK_UNITS) + ")")

_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_UNIT_ALT = ("%|％|分钟|平方米|平方千米|平方公里|公顷|㎡|m2|km2|公里|千米|米|秒|"
             "个|处|所|家|座|条|类|项|倍|人|岁|km|m|s")
_NUM_UNIT_RE = re.compile(r"[-+]?\d+(?:\.\d+)?\s*(?:" + _UNIT_ALT + r")?")


#: 日期 / 时间形态（**必须在抽数之前抹掉**，否则时间戳会被当成"编造的指标数值"误杀）
_DATE_TIME_RE = re.compile(
    r'\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日'          # 2026年10月14日
    r'|\d{4}\s*年\s*\d{1,2}\s*月(?!份)'                          # 2026年10月
    r'|\d{1,2}\s*月\s*\d{1,2}\s*日'                            # 10月14日
    r'|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[T\s]+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[+-]\d{2}:?\d{2}|Z)?)?'  # ISO / 斜杠日期
    r'|(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?'                              # 14:18:21 / 14:18
)
_DATE_PLACEHOLDER = "（时间）"


def strip_dates(text: str) -> str:
    """抹掉日期与时间形态，返回替换为占位符后的文本。

    "数据采集于 2026-10-14 14:18:21" 里的 2026 / 10 / 14 / 18 / 21 是**元数据**，不是体检指标；
    若不做这一步，LLM 每次如实回写采集时间都会被数值一致性校验误杀，导致概率性降级。
    """
    if not text:
        return ""
    return _DATE_TIME_RE.sub(_DATE_PLACEHOLDER, text)


def strip_clause_ids(text: str) -> str:
    """把条款编号、标准名与日期时间整体抹掉（避免这些字段里的数字被当成指标）。"""
    if not text:
        return ""
    out = re.sub(_SOURCE_TITLE, "《规范》", strip_dates(text))
    out = _CLAUSE_ID_RE.sub(" ", out)
    return out


def _norm_num(raw: str) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def extract_numbers(text: str, *, include_cjk: bool = True) -> list[tuple[float, str]]:
    """从文本抽出全部指标性数字。

    :return: [(数值, 原始片段), ...]，去重后按出现顺序返回
    """
    cleaned = strip_clause_ids(text or "")
    found: list[tuple[float, str]] = []
    seen: set[float] = set()
    for m in _NUM_UNIT_RE.finditer(cleaned):
        frag = m.group(0).strip()
        head = _NUM_RE.match(frag)
        val = _norm_num(head.group(0)) if head else None
        if val is None:
            continue
        key = round(val, 6)
        if key in seen:
            continue
        seen.add(key)
        found.append((val, frag))
    if include_cjk:
        for m in _CJK_NUM_RE.finditer(cleaned):
            val = float(_CJK_NUM.get(m.group(1), -1))
            if val < 0 or val in seen:
                continue
            seen.add(val)
            found.append((val, m.group(0)))
    return found


# --------------------------------------------------------------------------
# 报告数值集合
# --------------------------------------------------------------------------
def iter_numbers(obj: Any, _depth: int = 0) -> Iterable[float]:
    """递归收集报告 JSON 中的全部有限数值。"""
    if _depth > 12:
        return
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        f = float(obj)
        if f == f and abs(f) != float("inf"):
            yield f
        return
    if isinstance(obj, dict):
        for v in obj.values():
            yield from iter_numbers(v, _depth + 1)
        return
    if isinstance(obj, (list, tuple)):
        for v in obj:
            yield from iter_numbers(v, _depth + 1)


def _add_with_forms(out: set[float], v: float, *, with_percent: bool = True) -> None:
    """加入一个数及其「LLM 常见改写形态」。

    注意两点（都踩过坑，见 tests/test_validation.py 的回归用例）：
    1. **只加入不会造成语义跳跃的形态**。早期版本无条件加入 int(v)，
       导致 round(0.18)=0 这类噪声进入合法集合，让 0 附近的幻觉数字蒙混过关；
    2. **百分比形态要按字段语义决定**。评分（0~1）写成百分比是正常表达，
       但 uncertainty_band_ratio 这类「本来就是比例」的字段一旦展开，
       12% 就会等价于「12 个」，直接放行「菜市场 12 个」这种硬幻觉。
    """
    out.add(round(v, 6))
    if abs(v) >= 1.0:
        out.add(round(v))        # 整数形态（仅对 >= 1 的值安全）
        if abs(v) < 1000.0:
            out.add(round(v, 1))  # 一位小数形态
    else:
        out.add(round(v, 2))     # 小数值只保留 2 位，避免精度噪声
        out.add(round(v, 3))
    if with_percent and 0.0 <= v <= 1.0:
        out.add(round(v * 100, 6))
        out.add(round(v * 100, 1))
        out.add(round(v * 100))


def _is_ratio_key(key: str) -> bool:
    """判断字段是否「本身就是比例」（这类字段不展开百分比形态）。"""
    k = (key or "").lower()
    return k.endswith("ratio") or k.endswith("rate") or k.endswith("_pct")


def build_number_set(report: dict) -> set[float]:
    """构造报告的合法数值集合（含派生形态）。

    设计原则（很关键，别退化成「什么都放行」）：
    - **0~1 的评分才可以写成百分比**（如 coverage_score 0.18 -> 18）；
      而 uncertainty_band_ratio 这类**本来就是比例**的字段不展开，
      否则 12% 会等价于「12 个」，LLM 编造「菜市场 12 个」就能蒙混过关；
    - 面积同时接受 平方米 / 平方公里 两种写法（诊断里两种都会出现）；
    - 结构性常量只放「无 / 一 / 两」这类行文数量词。
    """
    out: set[float] = set()
    if not isinstance(report, dict):
        return out

    def add_value(value: Any, key: str = "") -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return
        f = float(value)
        if f != f or abs(f) == float("inf"):
            return
        _add_with_forms(out, f, with_percent=not _is_ratio_key(key))
        if "area_m2" in key or key.endswith("_m2"):
            # 平方米 <-> 平方公里（报告里两种单位都会被引用）
            _add_with_forms(out, f / 1e6)

    def mine_string(text: Any, key: str) -> None:
        """把字符串字段里出现的数字也纳入合法集合。

        典型场景：meta.generated_at = "2026-10-14T15:00:00+08:00"、
        meta.address = "上海市浦东新区海科路1650号" —— 这些是报告里的真实元数据，
        LLM 如实回写时不该被判成幻觉。
        """
        if not isinstance(text, str) or not text:
            return
        for v, _frag in extract_numbers(text):
            _add_with_forms(out, v)

    def walk(node: Any, key: str = "", depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, str(k), depth + 1)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v, key, depth + 1)
        elif isinstance(node, str):
            mine_string(node, key)
        else:
            add_value(node, key)

    # 1) 报告全部数值（cleaning_funnel 单独处理）
    fac = report.get("facilities")
    if isinstance(fac, dict):
        walk({k: v for k, v in fac.items() if k != "cleaning_funnel"}, "facilities", 0)
    for key in ("meta", "isochrones", "blindspots", "score", "siting"):
        walk(report.get(key), key, 0)

    # 2) 清洗漏斗：各级之间的比值是天然可引用的派生指标
    funnel = (report.get("facilities") or {}).get("cleaning_funnel") or {}
    fvals = sorted(
        float(v) for v in funnel.values()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    )
    for v in fvals:
        _add_with_forms(out, v)
    for i, a in enumerate(fvals):
        for b in fvals[i + 1:]:
            hi, lo = (a, b) if a >= b else (b, a)
            if lo > 0 and hi > 0:
                _add_with_forms(out, lo / hi * 100)         # 保留率百分比（inside/raw…）
                _add_with_forms(out, (1 - lo / hi) * 100)   # 剔除率百分比
                _add_with_forms(out, lo / hi)               # 保留率（0~1 形态）

    # 3) 等时圈面积之间的比值
    iso = report.get("isochrones") or {}
    areas = [float(v["area_m2"]) for v in iso.values()
             if isinstance(v, dict) and isinstance(v.get("area_m2"), (int, float))]
    for i, a in enumerate(areas):
        for b in areas[i + 1:]:
            hi, lo = (a, b) if a >= b else (b, a)
            if lo > 0:
                _add_with_forms(out, hi / lo)
                _add_with_forms(out, (hi - lo) / lo * 100)

    # 4) 地址里的门牌号（诊断文本常回写地址，属报告原值）
    addr = str((report.get("meta") or {}).get("address") or "")
    for v, _frag in extract_numbers(addr):
        _add_with_forms(out, v)

    # 5) 允许的结构性常量：只放「行文数量词」（无 / 一 / 两），不放具体门类数字。
    #    时间档位（5/10/15 分钟）与类别数（8 类）本身就在报告里，无需在此放行；
    #    若在此放行 3~100 的整数，LLM 编造「菜市场 12 个」将无法被拦截（回归用例见 tests/）。
    for extra in (0, 1, 2):
        _add_with_forms(out, float(extra))
    return out


def _tolerance(allowed: float) -> float:
    a = abs(allowed)
    if a <= 1.0:
        return 0.02
    if a <= 10.0:
        return 0.05
    if a < 100.0:
        return 0.6
    if a < 1000.0:
        return 3.0
    if a < 10000.0:
        return 30.0
    return 300.0


def _matches(value: float, allowed: set[float]) -> bool:
    for a in allowed:
        if abs(value - a) <= _tolerance(a):
            return True
    return False


def _flatten_text(payload: Any, _depth: int = 0) -> str:
    """把 dict/list/str 压成纯文本（dict 只取 value，不取 key，避免字段名干扰）。"""
    if _depth > 10:
        return ""
    if payload is None or isinstance(payload, bool):
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, (int, float)):
        return str(payload)
    if isinstance(payload, dict):
        return "\n".join(_flatten_text(v, _depth + 1) for v in payload.values())
    if isinstance(payload, (list, tuple)):
        return "\n".join(_flatten_text(v, _depth + 1) for v in payload)
    return str(payload)


def validate_numbers(
    payload: Any,
    report: dict,
    *,
    clauses: Sequence[str] | None = None,
    allowed: set[float] | None = None,
    max_mismatch_ratio: float = 0.0,
) -> dict:
    """校验 LLM 输出中出现的每个数字是否都来自报告。

    :param payload: LLM 输出（dict / list / str）
    :param report:  CONTRACT 4 的完整报告 dict
    :param clauses: 注入给模型的规范条文文本。**其中出现的数字视为"已给出"**——
        LLM 逐字引用「服务半径不宜大于 500 m」不是幻觉，而是忠实引用；
        但这两个数字（25 处菜市场 / 95.0 分）在任何条文里都不存在，因此仍会被拦下。
    :param allowed: 额外允许的数值集合
    :param max_mismatch_ratio: 允许的不匹配比例（默认 0 = 一个都不许错）
    :return: {"ok": bool, "mismatches": [...], "n_numbers": int, "reason": str}
    """
    text = _flatten_text(payload)
    numbers = extract_numbers(text)
    allowed_set = build_number_set(report) if isinstance(report, dict) else set()
    for clause_text in (clauses or []):
        for v, _frag in extract_numbers(str(clause_text)):
            _add_with_forms(allowed_set, v)
    if allowed:
        allowed_set |= {round(float(a), 6) for a in allowed}

    mismatches: list[dict] = []
    for value, frag in numbers:
        if not _matches(value, allowed_set):
            mismatches.append({"value": value, "text": frag})

    n = len(numbers)
    ratio = (len(mismatches) / n) if n else 0.0
    ok = ratio <= max_mismatch_ratio
    reason = ""
    if not ok:
        detail = "、".join(m["text"] for m in mismatches[:6])
        reason = (
            f"数值一致性校验失败：输出中 {len(mismatches)}/{n} 个数字无法在报告中找到依据（{detail}）"
            if n else "数值一致性校验失败：输出无可解析数字"
        )
    return {
        "ok": bool(ok),
        "mismatches": mismatches,
        "n_numbers": n,
        "mismatch_ratio": round(ratio, 4),
        "reason": reason,
    }


def validate_evidence(evidence: Sequence[dict], clauses: Sequence[Any] | None) -> dict:
    """校验 evidence 的出处是否忠实于检索到的条文（不新增、不伪造）。"""
    if clauses is None:
        return {"ok": True, "unknown": []}
    from .knowledge import citation_set  # 局部导入避免循环依赖

    allowed = citation_set(clauses)
    unknown: list[str] = []
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        src = str(item.get("source", "") or "").strip()
        if not src:
            unknown.append("(空出处)")
            continue
        if any(a and (a in src or src in a) for a in allowed):
            continue
        unknown.append(src)
    return {"ok": not unknown, "unknown": unknown}


# --------------------------------------------------------------------------
# 指标摘要（提示词 + 降级模板共用）
# --------------------------------------------------------------------------
def priority_rank(p: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(str(p or "").lower(), 1)


def fmt_seconds(sec: Any) -> str:
    """秒 -> 可读文本。

    括号内是**精确单位换算**（不是近似估计），因此不写「约」——
    否则降级模板的自检会把换算值判成无依据数字（见 fallback 的 numeric_check）。
    """
    if not isinstance(sec, (int, float)) or isinstance(sec, bool):
        return "数据不足"
    return f"{float(sec):.1f} 秒（{float(sec) / 60:.1f} 分钟）"


def fmt_area(m2: Any) -> str:
    """平方米 -> 可读文本（括号内为精确单位换算，理由同 fmt_seconds）。"""
    if not isinstance(m2, (int, float)) or isinstance(m2, bool):
        return "数据不足"
    return f"{float(m2):.1f} 平方米（{float(m2) / 1e6:.1f} 平方公里）"


def _as_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def build_metric_digest(report: dict, *, max_pois: int = 3, max_blindspots: int = 5) -> dict:
    """把报告压成「只含事实」的指标摘要（供提示词注入与模板降级共用）。"""
    report = report if isinstance(report, dict) else {}
    meta = report.get("meta") or {}
    center = meta.get("center") or {}
    quota = meta.get("quota") or {}

    isochrones = []
    for minutes, v in sorted((report.get("isochrones") or {}).items(),
                             key=lambda kv: _as_float(kv[0])):
        if not isinstance(v, dict):
            continue
        area = v.get("area_m2")
        isochrones.append({
            "minutes": _as_float(minutes),
            "area_m2": area,
            "area_km2": round(float(area) / 1e6, 4) if isinstance(area, (int, float)) else None,
            "n_samples": v.get("n_samples"),
            "routes_charged": v.get("routes_charged"),
            "uncertainty_band_ratio": v.get("uncertainty_band_ratio"),
        })

    categories = []
    for c in ((report.get("facilities") or {}).get("categories") or []):
        if not isinstance(c, dict):
            continue
        pois = [p for p in (c.get("pois") or []) if isinstance(p, dict)][:max_pois]
        categories.append({
            "key": c.get("key"),
            "name": c.get("name"),
            "count": c.get("count"),
            "density_per_km2": c.get("density_per_km2"),
            "nearest_walk_seconds": c.get("nearest_walk_seconds"),
            "coverage_score": c.get("coverage_score"),
            "top_pois": [
                {"name": p.get("name"), "walk_seconds": p.get("walk_seconds")} for p in pois
            ],
        })

    blind = report.get("blindspots") or {}
    blind_polys = []
    for b in (blind.get("polygons") or [])[:max_blindspots]:
        if isinstance(b, dict):
            blind_polys.append({"area_m2": b.get("area_m2"), "missing": b.get("missing")})

    score = report.get("score") or {}
    dimensions = []
    for d in (score.get("dimensions") or []):
        if isinstance(d, dict):
            dimensions.append({
                "key": d.get("key"), "name": d.get("name"),
                "score": d.get("score"), "weight": d.get("weight"),
                "raw_count": d.get("raw_count"),
            })

    siting = []
    for s in (report.get("siting") or [])[:5]:
        if isinstance(s, dict):
            siting.append({
                "lat": s.get("lat"), "lng": s.get("lng"),
                "reason": s.get("reason"), "gains": s.get("gains"),
            })

    return {
        "center": {"lat": center.get("lat"), "lng": center.get("lng")},
        "address": meta.get("address"),
        "data_mode": meta.get("data_mode"),
        "degraded": meta.get("degraded"),
        "degrade_reasons": list(meta.get("degrade_reasons") or []),
        "quota": {
            "routes_used": quota.get("routes_used"),
            "routes_limit": quota.get("routes_limit"),
            "poi_used": quota.get("poi_used"),
            "poi_limit": quota.get("poi_limit"),
        },
        "isochrones": isochrones,
        "categories": categories,
        "cleaning_funnel": dict((report.get("facilities") or {}).get("cleaning_funnel") or {}),
        "blindspot_total_area_m2": blind.get("total_area_m2"),
        "blindspots": blind_polys,
        "score_total": score.get("total"),
        "score_grade": score.get("grade"),
        "dimensions": dimensions,
        "siting": siting,
    }