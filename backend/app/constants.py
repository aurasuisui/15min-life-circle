"""8 类民生设施与检索词 —— **冻结**（CONTRACT.md §4.2，字段名不得更改）。

多关键字并集检索的计费口径已由 D1 闸门 G1 实测确认：**按【请求】计费**。
因此 8 类设施压到 2 次请求：

  * 医疗组（8 个关键字）：药店$药房$社区卫生服务中心$诊所$综合医院$养老院$敬老院$日间照料中心
  * 购物教育交通绿地区（9 个关键字）：农贸市场$菜市场$生鲜市场$超市$便利店$小学$公交车站$公园$广场

单次可召回 关键字数 × page_size(20) 条，远超单类 20 条上限。
"""
from __future__ import annotations

#: 盲区判定阈值：周边 1 km 内没有菜市场 / 药店 / 小学（任务书 FR-4 原文，按**步行距离**判定）
BLIND_SPOT_DISTANCE_M = 1000.0
#: 步行速度（m/s），与 core.isochrone.SampleConfig.walk_speed 同口径
WALK_SPEED_MPS = 1.25
#: 距离阈值折算成耗时阈值时用的等效速度（含路网绕行系数 1.3）：
#:   v_eff = 1.25 / 1.3 ≈ 0.9615 m/s  ->  T = 1000 / 0.9615 ≈ 1040 s
#: 这样「t > T」与「步行距离 > 1000 m」是同一个判定，与任务书口径一致。
BLIND_SPOT_DETOUR_FACTOR = 1.3
BLIND_SPOT_SECONDS = BLIND_SPOT_DISTANCE_M * BLIND_SPOT_DETOUR_FACTOR / WALK_SPEED_MPS  # 1040 s
#: 盲区判定的三类关键设施（任务书 FR-4 原文点名）
BLIND_SPOT_CATEGORIES = ("market", "pharmacy", "primary_school")

DEFAULT_MINUTES = (5, 10, 15)

#: key -> (中文名, 多关键字 query, 分组)
FACILITY_CATEGORIES: dict[str, dict[str, object]] = {
    "market": {
        "name": "菜市场",
        "query": "农贸市场$菜市场$生鲜市场",
        "group": "shopping_edu_transit_green",
    },
    "pharmacy": {
        "name": "药店",
        "query": "药店$药房",
        "group": "medical",
    },
    "primary_school": {
        "name": "小学",
        "query": "小学",
        "group": "shopping_edu_transit_green",
    },
    "clinic": {
        "name": "社区医疗",
        "query": "社区卫生服务中心$诊所$综合医院",
        "group": "medical",
    },
    "supermarket": {
        "name": "超市便利店",
        "query": "超市$便利店",
        "group": "shopping_edu_transit_green",
    },
    "elderly_care": {
        "name": "养老设施",
        "query": "养老院$敬老院$日间照料中心",
        "group": "medical",
    },
    "bus_stop": {
        "name": "公交站",
        "query": "公交车站",
        "group": "shopping_edu_transit_green",
    },
    "park": {
        "name": "公园绿地",
        "query": "公园$广场",
        "group": "shopping_edu_transit_green",
    },
}

CATEGORY_KEYS: tuple[str, ...] = tuple(FACILITY_CATEGORIES.keys())

#: 分组 -> 该组的检索词列表（合并后为 1 次请求）
GROUPS: dict[str, list[str]] = {
    "medical": [
        "药店", "药房", "社区卫生服务中心", "诊所", "综合医院", "养老院", "敬老院", "日间照料中心",
    ],
    "shopping_edu_transit_green": [
        "农贸市场", "菜市场", "生鲜市场", "超市", "便利店", "小学", "公交车站", "公园", "广场",
    ],
}
#: 分组 -> 该组覆盖的设施 key（顺序与 GROUPS 的关键字一一对应，用于模糊匹配兜底）
GROUP_KEYWORDS: dict[str, list[str]] = {
    "medical": ["药店", "药房", "社区卫生服务中心", "诊所", "综合医院", "养老院", "敬老院", "日间照料中心"],
    "shopping_edu_transit_green": ["农贸市场", "菜市场", "生鲜市场", "超市", "便利店", "小学", "公交车站", "公园", "广场"],
}
#: 关键字 -> 设施 key（用于「命中哪个关键字」的兜底归属）
KEYWORD_TO_CATEGORY: dict[str, str] = {
    "药店": "pharmacy", "药房": "pharmacy",
    "社区卫生服务中心": "clinic", "诊所": "clinic", "综合医院": "clinic",
    "养老院": "elderly_care", "敬老院": "elderly_care", "日间照料中心": "elderly_care",
    "农贸市场": "market", "菜市场": "market", "生鲜市场": "market",
    "超市": "supermarket", "便利店": "supermarket",
    "小学": "primary_school",
    "公交车站": "bus_stop",
    "公园": "park", "广场": "park",
}

#: 各类设施的参考密度（个/km²），用于密度项评分。
#: 依据《城市居住区规划设计标准》GB 50180-2018 与《完整居住社区建设标准（试行）》
#: 的设施配置要求折算（15 分钟生活圈 ≈ 3~5 km² 服务半径下的最低配建数量）。
DENSITY_REF_PER_KM2: dict[str, float] = {
    "market": 8.0,          # 每个菜市场服务 1~1.5 万人
    "pharmacy": 12.0,
    "primary_school": 4.0,
    "clinic": 4.0,
    "supermarket": 25.0,
    "elderly_care": 3.0,
    "bus_stop": 25.0,
    "park": 5.0,
}

#: 8 维评分权重（任务书点名三类关键设施 + 基础医疗权重更高）
SCORE_WEIGHTS: dict[str, float] = {
    "medical": 0.15,          # 药店 + 社区医疗 + 养老设施 归并
    "education": 0.15,
    "shopping": 0.15,
    "transport": 0.10,
    "green": 0.10,
    "elderly": 0.10,
    "pharmacy": 0.10,
    "market": 0.15,
}
#: 评分维度 -> 参与计算的设施 key
DIMENSION_CATEGORIES: dict[str, list[str]] = {
    "medical": ["clinic"],
    "education": ["primary_school"],
    "shopping": ["supermarket"],
    "transport": ["bus_stop"],
    "green": ["park"],
    "elderly": ["elderly_care"],
    "pharmacy": ["pharmacy"],
    "market": ["market"],
}
DIMENSION_NAMES: dict[str, str] = {
    "medical": "社区医疗",
    "education": "教育",
    "shopping": "购物",
    "transport": "交通",
    "green": "绿地",
    "elderly": "养老",
    "pharmacy": "药店",
    "market": "菜市场",
}

#: 评分公式权重（SOLUTION.md §4.5）
ACCESS_W, PRESENT_W, DENSITY_W = 0.45, 0.35, 0.20
#: 最近一处步行耗时的评分基准（秒）
ACCESS_REF_SECONDS = 900.0
#: 等级阈值
GRADE_BANDS = ((85.0, "A"), (70.0, "B"), (55.0, "C"), (0.0, "D"))
