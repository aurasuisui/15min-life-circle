"""POI 服务：多关键字合并检索 + 清洗流水线（CONTRACT §4.2 / SOLUTION §4.3）。

**2 次请求覆盖 8 类**（多关键字并集按【请求】计费，D1 闸门 G1 实测确认）：

  医疗组  药店$药房$社区卫生服务中心$诊所$综合医院$养老院$敬老院$日间照料中心   （8 个关键字）
  购物组  农贸市场$菜市场$生鲜市场$超市$便利店$小学$公交车站$公园$广场           （9 个关键字）

清流水线（6 级可插拔管道，每级留下可展示的计数与日志）：

  L1 归一化     全角→半角、去括号内容、去通用后缀、繁简与数字统一
  L2 精确去重   同 uid / (归一化名 + geohash7) 相同 -> 保留信息最全的一条
  L3 坐标漂移校正 同 uid 多次上报且相距 > 100 m -> 取众数簇中心（最多来源数、次近记录），
                 离群点被拉回并记录偏移量；单独出现的 uid 无法自证，只标记不移动
  L4 类型纠正   名称关键词 + 官方分类 tag **双证据**规则表；冲突时名称优先（名称更贴近真实业态）
  L5 圈内裁剪   点在实际输出的 15 分钟等时圈多边形内（本地 point-in-polygon，零配额）

⚠️ 关于 `cleaning_funnel.corrected`：包含**两类**修正 —— 剔除了错误记录（distractor）
与调整了归类（retag）。净变化量被有意记录下来，便于审计。
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from shapely.geometry import Point, Polygon
from shapely.prepared import prep

from ..constants import CATEGORY_KEYS, FACILITY_CATEGORIES, GROUPS, KEYWORD_TO_CATEGORY
from ..core.geo import haversine_m
from ..schemas.models import PlaceResult

# --------------------------------------------------------------------------
# L1 归一化
# --------------------------------------------------------------------------
_PUNCT = re.compile(r"\s+")
_BRACKETS = re.compile(r"[（(【\[].*?[)）】\]]")
_SUFFIXES = (
    "有限责任公司", "有限公司", "股份有限公司", "分公司", "旗舰店", "专卖店",
    "分店", "门店", "连锁店", "便利店", "超市", "药房", "药店", "菜市场", "农贸市场",
)
_FULLWIDTH = {ord(c): ord(c) - 0xFEE0 for c in "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"}


def normalize_name(name: str) -> str:
    """L1：名称归一化（用于去重键与相似度比较）。"""
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", str(name))
    s = s.translate(_FULLWIDTH)
    s = _BRACKETS.sub("", s)          # 去括号内容：（xx路店）
    s = _PUNCT.sub("", s)             # 折叠空白
    s = s.replace("(", "").replace(")", "").replace("（", "").replace("）", "")
    for suf in _SUFFIXES:             # 去通用后缀
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
    return s.strip().lower()


def geohash7(lat: float, lng: float, precision: int = 7) -> str:
    """极简 geohash（精度 7 ≈ 153 m × 153 m），只用于去重键，不用于展示。"""
    base32 = "0123456789bcdefghjkmnpqrstuvwxyz"
    lat_r, lng_r = (-90.0, 90.0), (-180.0, 180.0)
    bits = [16, 8, 4, 2, 1]
    bit, ch, even, out = 0, 0, True, []
    while len(out) < precision:
        if even:
            mid = (lng_r[0] + lng_r[1]) / 2
            if lng > mid:
                ch |= bits[bit]
                lng_r = (mid, lng_r[1])
            else:
                lng_r = (lng_r[0], mid)
        else:
            mid = (lat_r[0] + lat_r[1]) / 2
            if lat > mid:
                ch |= bits[bit]
                lat_r = (mid, lat_r[1])
            else:
                lat_r = (lat_r[0], mid)
        even = not even
        if bit < 4:
            bit += 1
        else:
            out.append(base32[ch])
            bit, ch = 0, 0
    return "".join(out)


# --------------------------------------------------------------------------
# L4 类型纠正规则表（名称关键词 + 官方分类 tag 双证据）
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CategoryRule:
    key: str
    tag_keywords: tuple[str, ...]
    name_keywords: tuple[str, ...] = ()
    name_exclude: tuple[str, ...] = ()


#: 权重：tag 命中 +2.0（官方分类权威）；名称命中 +1.5（名称更贴近真实业态，略低于 tag 但可推翻）
W_TAG, W_NAME = 2.0, 1.5
#: 判定阈值：低于此分不进任何类别（宁缺毋滥，避免把建材市场当菜市场）
MIN_SCORE = 1.4

RULES: tuple[CategoryRule, ...] = (
    CategoryRule("market",
                 ("菜市场", "农贸市场", "生鲜市场", "水产海鲜市场", "菜店", "市场", "商铺"),
                 ("菜市", "农贸", "菜场", "生鲜", "菜店"),
                 ("建材", "家具", "花鸟", "五金", "汽配", "钢材", "旧货", "古玩", "批发", "服装")),
    CategoryRule("pharmacy", ("药店", "药房", "中药店", "西药店", "医药"),
                 ("药房", "药店", "大药房", "医药")),
    CategoryRule("primary_school", ("小学", "九年一贯制学校"),
                 ("小学",),
                 ("培训", "教育科技", "咨询", "幼儿园", "托儿所", "成人", "亲子")),
    CategoryRule("clinic", ("社区卫生服务中心", "卫生院", "诊所", "综合医院", "社区医院", "卫生服务站"),
                 ("社区卫生服务", "社区医院", "卫生服务站", "地段医院", "门诊", "诊所", "卫生服务")),
    CategoryRule("supermarket", ("超市", "便利店", "大型超市", "果蔬超市", "生鲜超市", "便民商店"),
                 ("超市", "便利店", "便利", "生鲜", "全家", "罗森", "7-11", "711", "好德", "快客")),
    CategoryRule("elderly_care", ("养老服务", "福利机构", "疗养院", "敬老院", "养老院", "托老所", "日间照料"),
                 ("养老", "敬老", "日间照料", "颐养", "托老", "长者照护", "福利院")),
    CategoryRule("bus_stop", ("公交车站", "公交站", "地铁站", "轨道交通站"),
                 ("公交站", "公交车站", "地铁站", "轻轨站")),
    CategoryRule("park", ("公园", "绿地公园", "城市公园", "广场", "风景名胜"),
                 ("公园", "绿地", "广场", "花园")),
)


def _hit(text: str, words: Iterable[str]) -> bool:
    return any(w and w in text for w in words)


def _excluded(name: str) -> bool:
    """名称命中任一规则的 name_exclude -> 直接剔除（不参与任何类别）。"""
    return any(_hit(name or "", rule.name_exclude) for rule in RULES)


def _kw_in_name(name: str, kw: str) -> bool:
    """名称类关键词的匹配规则。

    ⚠️ 不能只做子串匹配：`张江生鲜超市` 同时含「生鲜」（菜市场类）与「超市」
    （超市类），子串匹配会让 tag 的 2.0 分把名称证据压过去，判成菜市场。
    真实业态里**结尾词**才是业态标识（…超市 是超市、…市场 是市场、
    …小学 是小学、…药房 是药店），所以名称命中必须有位置约束：
    结尾，或后面只跟括号/分店后缀。
    """
    if not kw or kw not in name:
        return False
    tail = name[name.rfind(kw) + len(kw):]
    return len(tail) <= 6 and not any(ch.isalnum() and "\u4e00" <= ch <= "\u9fff" for ch in tail)


def _name_score(name: str, rule: "CategoryRule") -> float:
    return W_NAME * sum(1 for kw in rule.name_keywords if _kw_in_name(name, kw))


_RULE_BY_KEY = {r.key: r for r in RULES}


def _rule(key: str) -> CategoryRule:
    return _RULE_BY_KEY[key]


def _tag_score(tag: str, rule: CategoryRule) -> float:
    return W_TAG if _hit(tag, rule.tag_keywords) else 0.0


def _kind_of(scores: dict[str, float]) -> str | None:
    """某个证据维度（tag 或 name）里得分最高的类别；全为 0 时返回 None。"""
    best_key, best_val = None, 0.0
    for key in CATEGORY_KEYS:
        val = scores.get(key, 0.0)
        if val > best_val:
            best_key, best_val = key, val
    return best_key


def _argmax(scores: dict[str, float]) -> tuple[str, float]:
    """并列时按 CATEGORY_KEYS 的固定顺序裁决，保证确定性。"""
    best_key, best_val = "", -1.0
    for key in CATEGORY_KEYS:
        val = scores.get(key)
        if val is not None and val > best_val:
            best_key, best_val = key, val
    return best_key, best_val


def classify_poi(name: str, tag: str, keyword_hint: str | None = None) -> tuple[str | None, float, str]:
    """L4：返回 (类别 key, 置信度, 依据说明)。key 为 None 表示应剔除。"""
    text = f"{tag} {name}"
    scores: dict[str, float] = defaultdict(float)
    reasons: dict[str, list[str]] = defaultdict(list)
    for rule in RULES:
        if _hit(name, rule.name_exclude):
            continue
        s = 0.0
        if _hit(tag, rule.tag_keywords):
            s += W_TAG
            reasons[rule.key].append(f"tag∈{rule.tag_keywords[0]}…")
        ns = _name_score(name, rule)
        if ns:
            s += ns
            hit_kw = next(kw for kw in rule.name_keywords if _kw_in_name(name, kw))
            reasons[rule.key].append(f"名称以「{hit_kw}」结尾")
        if s:
            scores[rule.key] = s

    if scores:
        tag_kind = _kind_of({k: _tag_score(tag, _rule(k)) for k in scores})
        name_kind = _kind_of({k: _name_score(name, _rule(k)) for k in scores})
        key, sc = _argmax(scores)
        # 冲突裁决：名称是业态最直接的证据（"张江生鲜超市" 的 tag 是「市场」，
        # 但招牌写的是超市）。tag 只在名称没有明确表态时作准。
        if tag_kind and name_kind and tag_kind != name_kind:
            key = name_kind
            sc = scores.get(name_kind, sc)
            reasons[key].append("名称证据推翻 tag（双证据冲突裁决）")
        if sc < MIN_SCORE:
            return None, sc, "双证据得分低于阈值"
        return key, sc, "；".join(reasons[key])

    if keyword_hint and keyword_hint in KEYWORD_TO_CATEGORY:
        return KEYWORD_TO_CATEGORY[keyword_hint], 0.6, f"仅检索词命中（{keyword_hint}），低置信"
    return None, 0.0, "无任何规则命中"


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class PoiRecord:
    uid: str
    name: str
    lat: float
    lng: float
    tag: str = ""
    address: str = ""
    source_group: str = ""
    keyword_hint: str = ""
    norm_name: str = ""
    category: str | None = None
    confidence: float = 0.0
    why: str = ""
    drift_m: float = 0.0
    corrected_xy: bool = False
    flags: list[str] = field(default_factory=list)

    def completeness(self) -> int:
        return sum(bool(x) for x in (self.uid, self.name, self.tag, self.address, self.lat, self.lng))

    def to_dict(self) -> dict:
        return {
            "uid": self.uid, "name": self.name, "lat": self.lat, "lng": self.lng,
            "tag": self.tag, "address": self.address, "category": self.category,
            "confidence": self.confidence, "why": self.why, "drift_m": self.drift_m,
            "flags": list(self.flags),
        }


@dataclass
class CleaningFunnelCounters:
    raw: int = 0
    deduped: int = 0
    corrected: int = 0
    inside: int = 0
    removed_wrong_type: int = 0
    retagged: int = 0
    drift_corrected: int = 0
    dropped_outside: int = 0

    def as_funnel(self) -> dict:
        return {"raw": self.raw, "deduped": self.deduped, "corrected": self.corrected, "inside": self.inside}


@dataclass
class CleaningResult:
    records: list[PoiRecord]
    counters: CleaningFunnelCounters
    log: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def by_category(self, key: str) -> list[PoiRecord]:
        return [r for r in self.records if r.category == key]


# --------------------------------------------------------------------------
# 检索 -> 原始记录
# --------------------------------------------------------------------------
def _extract_location(item: dict) -> tuple[float, float] | None:
    loc = item.get("location") or {}
    lat, lng = loc.get("lat"), loc.get("lng")
    if lat is None or lng is None:
        return None
    try:
        return float(lat), float(lng)
    except (TypeError, ValueError):
        return None


def fetch_raw(
    place_provider,
    center: tuple[float, float],
    radius: int,
    groups: tuple[str, ...] = ("medical", "shopping_edu_transit_green"),
    extra_pages: bool = False,
) -> tuple[list[PoiRecord], list[str], list[PlaceResult]]:
    """2 次请求覆盖 8 类。返回 (原始记录, 备注, 每次请求的元信息)。"""
    records: list[PoiRecord] = []
    notes: list[str] = []
    metas: list[PlaceResult] = []

    for gname in groups:
        keywords = GROUPS[gname]
        query = "$".join(keywords)
        resp = place_provider.search(query, center, radius=radius, page_num=0)
        results = list(resp.get("results") or [])
        meta = getattr(place_provider, "last_meta", {}) or {}
        metas.append(PlaceResult(
            query=query, results=results,
            source=str(meta.get("source", "unknown")),
            degraded=bool(meta.get("degraded")),
            reasons=list(meta.get("reasons") or []),
            radius=radius,
        ))

        kept = 0
        dropped = 0
        for item in results:
            loc = _extract_location(item)
            if loc is None:
                dropped += 1
                continue
            tag = str(item.get("tag") or (item.get("detail_info") or {}).get("tag") or "")
            name = str(item.get("name") or "")
            hint = next((k for k in keywords if k in name or k in tag), None)
            records.append(PoiRecord(
                uid=str(item.get("uid") or ""), name=name, lat=loc[0], lng=loc[1],
                tag=tag, address=str(item.get("address") or ""),
                source_group=gname, keyword_hint=hint or "",
                norm_name=normalize_name(name),
            ))
            kept += 1
        notes.append(f"检索组「{gname}」：{len(keywords)} 个关键字 1 次请求 -> {kept} 条有效（丢弃 {dropped} 条无坐标）")

        # 分页兜底：返回条数 == 关键字数 × page_size 说明可能被截断
        if extra_pages and len(results) >= len(keywords) * 20:
            resp2 = place_provider.search(query, center, radius=radius, page_num=1)
            for item in resp2.get("results") or []:
                loc = _extract_location(item)
                if loc is None:
                    continue
                tag = str(item.get("tag") or (item.get("detail_info") or {}).get("tag") or "")
                name = str(item.get("name") or "")
                hint = next((k for k in keywords if k in name or k in tag), None)
                records.append(PoiRecord(
                    uid=str(item.get("uid") or ""), name=name, lat=loc[0], lng=loc[1],
                    tag=tag, address=str(item.get("address") or ""),
                    source_group=gname, keyword_hint=hint or "", norm_name=normalize_name(name),
                ))
            notes.append(f"检索组「{gname}」触发分页兜底，追加 1 次请求")

    return records, notes, metas


# --------------------------------------------------------------------------
# L2 精确去重
# --------------------------------------------------------------------------
def dedupe(records: list[PoiRecord], log: list[str]) -> list[PoiRecord]:
    """同 uid（或 归一化名 + geohash7）判为同一条，保留信息最全的一条。"""
    by_uid: dict[str, list[PoiRecord]] = defaultdict(list)
    no_uid: list[PoiRecord] = []
    for r in records:
        if r.uid:
            by_uid[r.uid].append(r)
        else:
            no_uid.append(r)

    # 1) 同 uid 的多次上报【全部保留】，交给 L3 做漂移校正（这正是漂移的证据来源）
    merged: list[PoiRecord] = []
    for _uid, grp in by_uid.items():
        merged.extend(grp)

    # 2) 无 uid 的用 (归一化名, geohash7) 去重
    seen: dict[tuple[str, str], PoiRecord] = {}
    for r in no_uid:
        key = (r.norm_name, geohash7(r.lat, r.lng))
        prev = seen.get(key)
        if prev is None or r.completeness() > prev.completeness():
            seen[key] = r
    before = len(records)
    merged.extend(seen.values())
    log.append(f"L2 去重：{before} -> {len(merged)}")
    return merged


# --------------------------------------------------------------------------
# L2b 同名同地合并
# --------------------------------------------------------------------------
def dedupe_by_name_place(records: list[PoiRecord], log: list[str], max_m: float = 120.0) -> list[PoiRecord]:
    """`(归一化名, geohash7)` 相同的记录合并为一条（保留信息最全的）。

    L2 已经按 uid 去过一轮，但**故意保留了同 uid 的多次上报**（那是 L3 判断漂移的证据）。
    这一级才真正把同一条设施合并 —— 真实场景里跨两个检索组回来的重复就在这里被吃掉。
    """
    buckets: dict[tuple[str, str, str], list[PoiRecord]] = defaultdict(list)
    for r in records:
        buckets[(r.norm_name or r.name, geohash7(r.lat, r.lng), r.category or "")].append(r)

    out: list[PoiRecord] = []
    merged = 0
    for (_n, _g, _c), grp in buckets.items():
        if len(grp) == 1:
            out.append(grp[0])
            continue
        grp.sort(key=lambda r: (r.corrected_xy, r.completeness()), reverse=True)
        keep = grp[0]
        for other in grp[1:]:
            d = haversine_m((keep.lat, keep.lng), (other.lat, other.lng))
            if d <= max_m:
                merged += 1
                keep.flags.append("merged_dup")
                if other.address and not keep.address:
                    keep.address = other.address
            else:
                out.append(other)
        out.append(keep)
    if merged:
        log.append(f"L2b 同名同地合并：{merged} 条重复被合并")
    return out


# --------------------------------------------------------------------------
# L3 坐标漂移校正
# --------------------------------------------------------------------------
def correct_drift(
    records: list[PoiRecord], log: list[str], outlier_m: float = 250.0, min_sources: int = 2
) -> int:
    """同 uid 出现多次且相距 > 100 m 时，取「最多来源数、次近距离」的簇中心为权威坐标。

    单独出现的 uid 无法自证漂移，只打标记不移动 —— 这是诚实做法。
    """
    fixed = 0
    by_uid: dict[str, list[PoiRecord]] = defaultdict(list)
    for r in records:
        by_uid[r.uid].append(r)

    for uid, grp in by_uid.items():
        if not uid or len(grp) < min_sources:
            for r in grp:
                r.flags.append("single_source")
            continue
        # 用「与其余点距离中位数最小」的点作为锚点，避免被离群点带偏
        best, best_med = grp[0], float("inf")
        for cand in grp:
            ds = sorted(haversine_m((cand.lat, cand.lng), (o.lat, o.lng)) for o in grp)
            med = ds[len(ds) // 2]
            if med < best_med:
                best, best_med = cand, med
        for r in grp:
            d = haversine_m((r.lat, r.lng), (best.lat, best.lng))
            r.drift_m = round(d, 1)
            if d > 100.0 and r is not best:
                r.flags.append("drift_outlier")
                if d > outlier_m:
                    r.flags.append("drift_far")
                    r.lat, r.lng = best.lat, best.lng
                    r.corrected_xy = True
                    fixed += 1
    if fixed:
        log.append(f"L3 坐标漂移校正：{fixed} 条记录被拉回权威坐标")
    return fixed


# --------------------------------------------------------------------------
# L4 类型纠正
# --------------------------------------------------------------------------
def correct_types(
    records: list[PoiRecord], log: list[str], counters: CleaningFunnelCounters
) -> list[PoiRecord]:
    kept: list[PoiRecord] = []
    for r in records:
        key, conf, why = classify_poi(r.name, r.tag, r.keyword_hint)
        if key is None or _excluded(r.name):
            counters.removed_wrong_type += 1
            r.flags.append("removed_wrong_type")
            continue
        r.confidence, r.why = round(conf, 2), why
        r.category = key
        if conf < 0.95:
            r.flags.append("type_uncertain")
        kept.append(r)
    log.append(f"L4 类型纠正：剔除 {counters.removed_wrong_type} 条（类型错标 / 非目标业态）")
    return kept


# --------------------------------------------------------------------------
# L5 圈内裁剪
# --------------------------------------------------------------------------
def clip_inside(records: list[PoiRecord], polygon_lnglat: list[list[tuple[float, float]]]):
    """返回 (圈内, 圈外) 两个列表。polygon 为 BD09 [(lng,lat)...]，零配额。"""
    if not polygon_lnglat or len(polygon_lnglat[0]) < 3:
        return [], list(records)
    poly = Polygon([(lng, lat) for lng, lat in polygon_lnglat[0]]).buffer(0)
    prepared = prep(poly)
    inside, outside = [], []
    for r in records:
        (inside if prepared.contains(Point(r.lng, r.lat)) else outside).append(r)
    return inside, outside


# --------------------------------------------------------------------------
# 全流程
# --------------------------------------------------------------------------
def build_cleaning_result(
    raw: list[PoiRecord],
    polygon_lnglat: list[list[tuple[float, float]]] | None,
    log: list[str] | None = None,
) -> CleaningResult:
    log = log if log is not None else []
    counters = CleaningFunnelCounters(raw=len(raw))
    log.append(f"L0/L1 源头收敛 + 归一化：原始召回 {len(raw)} 条")

    merged = dedupe(raw, log)
    counters.drift_corrected = correct_drift(merged, log)
    merged = dedupe_by_name_place(merged, log)
    counters.deduped = len(merged)

    kept = correct_types(merged, log, counters)
    counters.corrected = len(kept)
    log.append(
        f"L5 类型分布："
        + "，".join(f"{k}={sum(1 for r in kept if r.category == k)}" for k in CATEGORY_KEYS)
    )

    if polygon_lnglat:
        inside, outside = clip_inside(kept, polygon_lnglat)
        counters.dropped_outside = len(outside)
        log.append(f"L6 圈内裁剪：{len(kept)} -> {len(inside)}（圈外 {len(outside)} 条，留作临界设施）")
    else:
        inside = kept
        log.append("L6 圈内裁剪：无有效等时圈，跳过滤除")
    counters.inside = len(inside)

    # 归类纠正 = 最终类别 ≠ **tag 所主张**的类别。
    # 用 tag 而不是检索词作基准：检索词是「召回工具」，同一条 POI 常被多个关键词召回
    # （例如「张江生鲜超市」被 tag 里的「市场」召回），拿它当基准会漏报或误报；
    # 而 tag 是百度给出的**分类主张**，正是 L5 规则要推翻的对象。
    retag_examples: list[str] = []
    for r in kept:
        claimed, _conf, _why = classify_poi(r.name, r.tag, None)
        if r.category and claimed and claimed != r.category:
            counters.retagged += 1
            r.flags.append(f"retagged_from:{claimed}")
            if len(retag_examples) < 5:
                retag_examples.append(f"{r.name}（tag 主张 {claimed} -> 实际 {r.category}）")
    if retag_examples:
        log.append(f"L5 归类纠正 {counters.retagged} 条，示例：" + "；".join(retag_examples))
    log.append(
        f"清洗漏斗：raw={counters.raw} -> deduped={counters.deduped} -> corrected={counters.corrected} "
        f"-> inside={counters.inside}"
        f"（其中剔除错误业态 {counters.removed_wrong_type} 条、纠正归类 {counters.retagged} 条、"
        f"坐标漂移校正 {counters.drift_corrected} 条、圈外 {counters.dropped_outside} 条）"
    )
    return CleaningResult(records=inside, counters=counters, log=log)
