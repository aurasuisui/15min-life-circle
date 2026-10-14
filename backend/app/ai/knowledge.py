"""规范条文知识库（RAG 检索源）。

收录四份与「15 分钟社区生活圈」直接相关的公开规范／导则／指南，
条目为**条文摘要**（非逐字全文），每条含：

- id       稳定标识（标准代号#条号）
- text     条文摘要
- source   出处（标准全称 + 条号）
- features 可服务的民生设施类别（对齐 CONTRACT.md 4.2 的 8 类 key）
- keywords 关键词（检索用）

检索采用**关键词 + 类别特征打分**（无需向量库、零依赖、可解释、可复现），
符合「所有结论可追溯到一条公开条文」的要求，避免 LLM 凭空引用规范。

> 注意：摘要用于 RAG 提示与出处标注，个别数值为工程近似；
> 正式引用请以标准原文为准。本项目仅用其做「解释与归因」，不参与任何数值计算。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

__all__ = [
    "Clause",
    "CLAUSES",
    "FEATURE_KEYS",
    "CATEGORY_TO_FEATURE",
    "CATEGORY_NAMES",
    "search",
    "format_clauses",
    "citation_set",
    "get_clause",
    "display_name",
]

# --------------------------------------------------------------------------
# 设施类别（CONTRACT.md 4.2 冻结）
# --------------------------------------------------------------------------
FEATURE_KEYS: tuple[str, ...] = (
    "market",
    "pharmacy",
    "primary_school",
    "clinic",
    "supermarket",
    "elderly_care",
    "bus_stop",
    "park",
    "_overall",
    "_blindspot",
    "_siting",
    "_quota",
)

#: 设施 key -> 知识库 feature
CATEGORY_TO_FEATURE: dict[str, str] = {
    "market": "market",
    "pharmacy": "pharmacy",
    "primary_school": "primary_school",
    "clinic": "clinic",
    "supermarket": "supermarket",
    "elderly_care": "elderly_care",
    "bus_stop": "bus_stop",
    "park": "park",
}

#: key -> 中文名（报告里若缺 name 时兜底）
CATEGORY_NAMES: dict[str, str] = {
    "market": "菜市场",
    "pharmacy": "药店",
    "primary_school": "小学",
    "clinic": "社区医疗",
    "supermarket": "超市便利店",
    "elderly_care": "养老设施",
    "bus_stop": "公交站",
    "park": "公园绿地",
}

_STANDARD_NAME: dict[str, str] = {
    "GB50180": "《城市居住区规划设计标准》GB 50180-2018",
    "WZSB": "《完整居住社区建设标准（试行）》",
    "SH15": "《上海市15分钟社区生活圈规划导则》",
    "SWB": "商务部《城市一刻钟便民生活圈建设指南》",
}


@dataclass(frozen=True)
class Clause:
    """一条规范条文。"""

    id: str
    text: str
    source: str
    features: frozenset[str] = field(default_factory=frozenset)
    keywords: tuple[str, ...] = ()
    #: 命中该条时建议的动作类型（供模板降级与提示词使用）
    action: str = ""

    @property
    def code(self) -> str:
        """标准代号（GB50180 / SH15 ...）。"""
        return self.id.split("#", 1)[0]

    @property
    def standard(self) -> str:
        return _STANDARD_NAME.get(self.code, self.source)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "source": self.source,
            "features": sorted(self.features),
            "action": self.action,
        }


def _c(
    cid: str,
    text: str,
    source: str,
    features: Iterable[str],
    keywords: Iterable[str],
    action: str = "",
) -> Clause:
    return Clause(
        id=cid,
        text=text,
        source=source,
        features=frozenset(features),
        keywords=tuple(keywords),
        action=action,
    )


# --------------------------------------------------------------------------
# 条文（4 份文件，共 34 条）
# --------------------------------------------------------------------------
CLAUSES: tuple[Clause, ...] = (
    # ---- GB 50180-2018 《城市居住区规划设计标准》 ----
    _c("GB50180#3.0.4",
       "十五分钟生活圈居住区按步行距离约 800~1000 m、对应步行时间约 15 分钟划分；十分钟生活圈居住区对应步行距离约 500 m、约 10 分钟；"
       "五分钟生活圈居住区对应步行距离约 300 m、约 5 分钟。各级生活圈的划分以居民步行到达基本公共服务设施的实际距离为依据。",
       "《城市居住区规划设计标准》GB 50180-2018 第 3.0.4 条（15/10/5 分钟生活圈划分）",
       ["_overall", "_blindspot"],
       ["15分钟", "生活圈", "步行距离", "可达性", "划分", "等级"], "tier"),
    _c("GB50180#5.0.1",
       "居住区公共服务设施应按照生活圈分级配置，并满足居民日常生活的便利性与可达性要求；设施布局宜结合步行系统与公共空间布置，"
       "使居民在相应生活圈层内步行可达。",
       "《城市居住区规划设计标准》GB 50180-2018 第 5.0.1 条（公共服务设施配置原则）",
       ["_overall", "_siting"],
       ["公共服务设施", "分级配置", "步行", "布局", "可达"], "siting"),
    _c("GB50180#5.0.2",
       "十五分钟生活圈居住区配套设施宜包括初中、社区医院、养老院、社区文化中心、社区体育中心、综合超市、农贸市场、社区服务中心等，"
       "服务半径不宜大于 1000 m；十分钟生活圈宜包括小学、菜市场、餐饮、药店、健身场馆等，服务半径不宜大于 500 m；"
       "五分钟生活圈宜包括幼儿园、便利店、社区食堂、社区卫生服务站、日间照料中心等，服务半径不宜大于 300 m。",
       "《城市居住区规划设计标准》GB 50180-2018 第 5.0.2 条（配套设施分级配置表）",
       ["market", "pharmacy", "primary_school", "clinic", "supermarket", "elderly_care", "park", "_overall"],
       ["服务半径", "配套设施", "农贸市场", "药店", "小学", "社区医院", "养老院", "超市", "分级"], "coverage"),
    _c("GB50180#5.0.3",
       "配套设施用地与建筑宜集中布置形成社区综合服务中心，并宜与公交站点、公共绿地、步行通道结合设置，提高一次出行完成多项活动的便利程度。",
       "《城市居住区规划设计标准》GB 50180-2018 第 5.0.3 条（配套设施布局方式）",
       ["bus_stop", "park", "_siting"],
       ["综合服务中心", "集中布置", "公交站点", "公共绿地", "步行通道"], "siting"),
    _c("GB50180#6.0.2",
       "居住区道路系统应满足消防、救护等应急救援车辆的通行要求，并应保障步行与骑行系统的连续性与安全性；"
       "步行系统宜与居住区公共绿地、公共服务设施连通。",
       "《城市居住区规划设计标准》GB 50180-2018 第 6.0.2 条（道路与步行系统）",
       ["_blindspot", "_overall"],
       ["道路", "步行系统", "连通", "安全", "消防"], "connectivity"),
    _c("GB50180#6.0.4",
       "居住区应设置应急避难场所与疏散通道，并应符合城市综合防灾要求，避难场所宜结合公共绿地、学校操场等开敞空间设置。",
       "《城市居住区规划设计标准》GB 50180-2018 第 6.0.4 条（防灾避难）",
       ["park", "primary_school"],
       ["避难场所", "疏散通道", "公共绿地", "学校操场", "防灾"], "resilience"),

    # ---- 《完整居住社区建设标准（试行）》 ----
    _c("WZSB#1.1",
       "完整居住社区以居民步行 5~10 分钟可达为基本要求，配置基本公共服务设施、便民商业服务设施、市政配套基础设施、"
       "公共活动空间、物业管理服务和社区管理机制六大类建设内容，实现「服务设施齐全、环境宜居、管理有序」。",
       "《完整居住社区建设标准（试行）》第一部分（建设目标与六大类内容）",
       ["_overall", "_blindspot"],
       ["完整居住社区", "5分钟", "10分钟", "六大类", "基本公共服务"], "tier"),
    _c("WZSB#2.1",
       "完整居住社区应配置幼儿园（托儿所），服务半径不宜大于 300 m，为 3~6 岁幼儿提供就近入园条件；规模较小时可多个社区联合设置。",
       "《完整居住社区建设标准（试行）》第二部分（基本公共服务设施 · 幼儿园）",
       ["primary_school"],
       ["幼儿园", "托儿所", "服务半径", "300米", "入园"], "coverage"),
    _c("WZSB#2.2",
       "完整居住社区应配置老年人日间照料中心（托老所）与社区养老服务站，为老年人提供日间照料、助餐、康复保健等服务，"
       "宜与社区卫生服务站、社区食堂组合设置，形成社区综合为老服务中心。",
       "《完整居住社区建设标准（试行）》第二部分（基本公共服务设施 · 养老）",
       ["elderly_care", "clinic"],
       ["日间照料", "养老服务站", "助餐", "康复", "为老服务"], "elderly"),
    _c("WZSB#2.3",
       "完整居住社区应配置社区卫生服务站，提供基本医疗、公共卫生与健康管理服务；宜与社区养老、日间照料设施邻近设置，实现医养结合。",
       "《完整居住社区建设标准（试行）》第二部分（基本公共服务设施 · 社区卫生服务站）",
       ["clinic", "pharmacy"],
       ["社区卫生服务站", "基本医疗", "公共卫生", "医养结合"], "medical"),
    _c("WZSB#3.1",
       "便民商业服务设施应包含综合超市（或生鲜超市）、便利店、药店、理发店、洗衣店、维修点、快递服务等内容，"
       "宜集中布局形成社区商业中心，步行 5 分钟可达。",
       "《完整居住社区建设标准（试行）》第三部分（便民商业服务设施）",
       ["supermarket", "market", "pharmacy"],
       ["便民商业", "综合超市", "便利店", "药店", "快递", "社区商业"], "commerce"),
    _c("WZSB#3.2",
       "社区应提供邮件快件寄递、家电维修、家政服务等便民服务，可结合社区综合服务设施或商业设施设置，并应满足无障碍通行要求。",
       "《完整居住社区建设标准（试行）》第三部分（便民服务 · 寄递与维修）",
       ["supermarket", "_siting"],
       ["寄递", "快递", "维修", "家政", "无障碍"], "commerce"),
    _c("WZSB#4.1",
       "完整居住社区应配置公共活动场地与绿地，绿地率不宜低于 25%，并应设置适合老年人、儿童活动的场地与设施，"
       "公共活动场地步行 5~10 分钟可达。",
       "《完整居住社区建设标准（试行）》第四部分（公共活动空间）",
       ["park"],
       ["公共活动场地", "绿地", "绿地率", "老年人", "儿童"], "park"),
    _c("WZSB#5.1",
       "市政配套基础设施应包括供水、排水、供电、通信、燃气、环卫等，并应实现无障碍设施全覆盖，保障轮椅、婴儿车通行。",
       "《完整居住社区建设标准（试行）》第五部分（市政配套基础设施）",
       ["_overall", "bus_stop"],
       ["市政", "无障碍", "轮椅", "婴儿车", "环卫"], "accessibility"),
    _c("WZSB#6.1",
       "社区应建立物业管理与社区管理机制，落实设施维护、环境整治与安全管理责任，保障各类服务设施持续有效运行。",
       "《完整居住社区建设标准（试行）》第六部分（物业管理与社区管理）",
       ["_overall"],
       ["物业管理", "社区管理", "设施维护", "安全"], "governance"),

    # ---- 《上海市 15 分钟社区生活圈规划导则》 ----
    _c("SH15#2.1",
       "上海 15 分钟社区生活圈以 15 分钟步行可达（约 800~1000 m）为空间尺度，配置基本保障类、品质提升类、特色引导类三类设施；"
       "基本保障类设施应满足 5~10 分钟可达，品质提升类设施宜满足 15 分钟可达。",
       "《上海市15分钟社区生活圈规划导则》第 2 章（总体要求 · 三类设施）",
       ["_overall", "_blindspot"],
       ["15分钟", "社区生活圈", "基本保障类", "品质提升类", "步行可达"], "tier"),
    _c("SH15#3.1",
       "城镇社区应配置社区文化活动中心（或文化活动室）、社区体育活动场地、社区公园等公共空间设施，"
       "并宜与社区商业、为老服务设施复合设置，提高设施使用效率。",
       "《上海市15分钟社区生活圈规划导则》第 3 章（公共空间与文体设施）",
       ["park", "_siting"],
       ["文化活动中心", "体育场地", "社区公园", "复合设置", "公共空间"], "park"),
    _c("SH15#3.2",
       "社区商业设施应重点保障菜市场（生鲜超市）、综合超市、便利店、药店等日常购物需求，"
       "菜市场服务半径宜控制在 500 m 以内，鼓励与社区综合服务中心一体化设置。",
       "《上海市15分钟社区生活圈规划导则》第 3 章（社区商业 · 菜市场）",
       ["market", "supermarket", "pharmacy"],
       ["菜市场", "生鲜超市", "服务半径", "500米", "日常购物"], "market"),
    _c("SH15#4.1",
       "社区为老服务设施应包括综合为老服务中心、长者照护之家、日间照护中心、社区食堂与助餐点，"
       "宜临近社区卫生服务站与公园绿地设置，形成「医养+休闲」组合。",
       "《上海市15分钟社区生活圈规划导则》第 4 章（为老服务设施）",
       ["elderly_care", "clinic", "park"],
       ["为老服务中心", "长者照护", "日间照护", "社区食堂", "助餐"], "elderly"),
    _c("SH15#4.2",
       "社区卫生服务中心（站）应结合社区生活圈均衡布局，步行 15 分钟可达；宜与养老、康复、日间照料设施形成医养结合服务网络。",
       "《上海市15分钟社区生活圈规划导则》第 4 章（医疗卫生设施）",
       ["clinic", "pharmacy", "elderly_care"],
       ["社区卫生服务中心", "医疗卫生", "医养结合", "均衡布局"], "medical"),
    _c("SH15#4.3",
       "社区教育设施应包括幼儿园、小学等基础教育设施，宜与社区文化、体育设施统筹布局，并保障学生步行通学的安全与便捷。",
       "《上海市15分钟社区生活圈规划导则》第 4 章（教育设施）",
       ["primary_school"],
       ["幼儿园", "小学", "基础教育", "步行通学", "安全"], "education"),
    _c("SH15#5.1",
       "社区慢行系统应连续成网，步行通道宜满足无障碍要求，并应结合林荫道、街角广场提升步行舒适度；"
       "步行网络断点应通过增设过街设施、开放单位内部通道等方式打通。",
       "《上海市15分钟社区生活圈规划导则》第 5 章（慢行与无障碍）",
       ["_blindspot", "bus_stop"],
       ["慢行系统", "步行通道", "无障碍", "断点", "过街设施"], "connectivity"),
    _c("SH15#6.1",
       "社区生活圈评估应建立「设施覆盖率、步行可达性、服务盲区」三类指标，以量化评估结果指导设施补缺与更新，"
       "并应公示评估结论、吸纳居民意见。",
       "《上海市15分钟社区生活圈规划导则》第 6 章（评估与行动）",
       ["_overall", "_blindspot", "_siting"],
       ["评估", "覆盖率", "步行可达性", "服务盲区", "补缺", "公众参与"], "assessment"),
    _c("SH15#6.2",
       "设施补缺应优先补齐基本保障类设施的缺口，并优先向老年人口、儿童人口比重高及现状设施覆盖薄弱的区域投放，"
       "实现社区间基本公共服务均等化。",
       "《上海市15分钟社区生活圈规划导则》第 6 章（补缺优先序）",
       ["_siting", "_overall"],
       ["补缺", "优先", "基本保障类", "均等化", "薄弱区域"], "siting"),
    _c("SH15#7.1",
       "社区绿道与公园绿地应步行 5~15 分钟可达，并宜与社区公共活动场地、健身步道串联，形成连续的开放空间网络。",
       "《上海市15分钟社区生活圈规划导则》第 7 章（绿地开放空间）",
       ["park"],
       ["绿道", "公园绿地", "健身步道", "开放空间", "串联"], "park"),
    _c("SH15#8.1",
       "公共交通站点宜位于社区生活圈中心 500 m 步行范围内，并应保障站点至主要公共服务设施、居住组团的步行通道连续、安全。",
       "《上海市15分钟社区生活圈规划导则》第 8 章（公共交通衔接）",
       ["bus_stop"],
       ["公共交通", "公交站点", "500米", "步行通道", "接驳"], "transit"),

    # ---- 商务部《城市一刻钟便民生活圈建设指南》 ----
    _c("SWB#1.1",
       "城市一刻钟便民生活圈是以社区(小区)为圆心、服务半径为步行 15 分钟左右的范围内，以满足居民日常生活基本消费和品质消费为目标，"
       "多业态集聚形成的社区商圈。",
       "商务部《城市一刻钟便民生活圈建设指南》（总体要求 · 定义）",
       ["_overall", "_blindspot"],
       ["一刻钟", "便民生活圈", "15分钟", "社区商圈", "基本消费"], "tier"),
    _c("SWB#2.1",
       "便民生活圈应优先配齐基本保障类业态，包括社区综合超市、便利店、菜市场（生鲜超市）、药店、早餐店、维修点、"
       "美容美发店、洗衣店、照相文印店、家政服务点、再生资源回收点等。",
       "商务部《城市一刻钟便民生活圈建设指南》（基本保障类业态）",
       ["market", "supermarket", "pharmacy"],
       ["基本保障类", "综合超市", "便利店", "菜市场", "生鲜超市", "药店", "早餐店"], "commerce"),
    _c("SWB#2.2",
       "便民生活圈应因地制宜发展品质提升类业态，包括特色餐饮、运动健身、保健养生、教育培训、社区养老、托育服务、"
       "图书阅览、休闲娱乐等，满足居民多样化消费需求。",
       "商务部《城市一刻钟便民生活圈建设指南》（品质提升类业态）",
       ["elderly_care", "park", "primary_school"],
       ["品质提升类", "运动健身", "教育培训", "社区养老", "托育", "图书阅览"], "commerce"),
    _c("SWB#3.1",
       "便民生活圈应科学优化布局，结合社区人口结构、消费习惯与设施现状，合理确定业态规模与配置标准，"
       "并加强线上线下一体化服务能力建设。",
       "商务部《城市一刻钟便民生活圈建设指南》（优化布局）",
       ["_siting"],
       ["优化布局", "人口结构", "消费习惯", "配置标准", "线上线下"], "siting"),
    _c("SWB#3.2",
       "应加强便民生活圈设施的动态监测与评估，定期开展居民满意度调查，及时补齐设施短板，"
       "对缺失业态通过新建、改建、共享等方式补足。",
       "商务部《城市一刻钟便民生活圈建设指南》（动态监测与补足）",
       ["_overall", "_blindspot", "_siting"],
       ["动态监测", "评估", "满意度", "补齐短板", "共享"], "assessment"),
    _c("SWB#4.1",
       "便民生活圈应推进适老化改造，配置无障碍设施与休息座椅，鼓励设置长者食堂、代买代送等服务，"
       "保障老年人、残疾人等群体便利获得生活服务。",
       "商务部《城市一刻钟便民生活圈建设指南》（适老化与无障碍）",
       ["elderly_care", "market", "pharmacy"],
       ["适老化", "无障碍", "长者食堂", "代买代送", "老年人"], "elderly"),
)


# --------------------------------------------------------------------------
# 检索
# --------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,4}")
_STOPWORDS = frozenset({"的", "和", "与", "及", "在", "为", "应", "宜", "或", "等", "是", "以", "并", "对", "中", "内"})


def _tokens(query: str) -> list[str]:
    """极简中文切分：英文词 + 连续汉字 2~4 字滑窗（无需 jieba 等外部依赖）。"""
    out: list[str] = []
    for raw in _TOKEN_RE.findall(query or ""):
        if re.fullmatch(r"[A-Za-z0-9_]+", raw):
            out.append(raw.lower())
            continue
        for n in (4, 3, 2):
            for i in range(0, max(len(raw) - n + 1, 0)):
                frag = raw[i : i + n]
                if frag not in _STOPWORDS:
                    out.append(frag)
        if len(raw) <= 4:
            out.append(raw)
    return out


def _score(clause: Clause, features: frozenset[str], qset: set[str]) -> float:
    """打分：类别特征命中为主，关键词命中为辅。"""
    score = 0.0
    hit = features & clause.features
    score += 6.0 * len(hit)
    if "_overall" in features and "_overall" in clause.features:
        score += 2.0  # 总体类条款作为背景知识保留
    for kw in clause.keywords:
        kl = kw.lower()
        if kl in qset:
            score += 1.5
        else:
            for q in qset:
                if len(q) >= 2 and (q in kl or kl in q):
                    score += 0.8
                    break
    return score


def search(
    features: Sequence[str] | str | None = None,
    query: str = "",
    k: int = 6,
) -> list[Clause]:
    """检索最相关的 k 条规范条文。

    :param features: 设施类别 key（CONTRACT 4.2）或知识库 feature；None = 不限
    :param query:    额外自由文本（诊断文本、设施名等）
    :param k:        返回条数
    :return: 按相关性降序的条文列表；**永不为空**（无命中时回退到总体类条款）
    """
    if isinstance(features, str):
        features = [features]
    feat = frozenset(CATEGORY_TO_FEATURE.get(f, f) for f in (features or []) if f)
    qset = set(_tokens(query))

    scored: list[tuple[float, int, Clause]] = []
    for idx, clause in enumerate(CLAUSES):
        s = _score(clause, feat, qset)
        # 同分时保持稳定顺序（可复现）
        scored.append((s, -idx, clause))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)

    picked = [c for s, _, c in scored if s > 0][: max(k, 1)]
    if not picked:
        picked = [c for c in CLAUSES if c.features & feat][: max(k, 1)]
    if not picked:
        picked = [c for c in CLAUSES if "_overall" in c.features][: max(k, 1)]
    return picked


def format_clauses(clauses: Sequence[Clause], max_chars: int = 4000) -> str:
    """把条文格式化成可注入提示词的文本块（带编号，供 LLM 用 clause 字段引用）。"""
    lines: list[str] = []
    used = 0
    for i, c in enumerate(clauses, 1):
        line = f"[{i}] {c.text}\n    出处：{c.source}\n    引用格式：{c.standard}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines) if lines else "（知识库无匹配条文，只能输出『数据不足』，不得编造规范依据。）"


def citation_set(clauses: Sequence[Clause]) -> set[str]:
    """允许出现的出处字符串集合（用于校验 LLM 的 source 字段是否忠实）。"""
    out: set[str] = set()
    for c in clauses:
        out.add(c.source)
        out.add(c.standard)
        out.add(c.code)
        out.add(c.id)
    return out


def get_clause(cid: str) -> Clause | None:
    """按 id 取条文（供测试与降级模板使用）。"""
    for c in CLAUSES:
        if c.id == cid:
            return c
    return None


def display_name(key: str) -> str:
    """设施 key -> 中文名。"""
    return CATEGORY_NAMES.get(key, key)
