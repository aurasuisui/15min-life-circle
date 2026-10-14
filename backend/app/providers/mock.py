"""MockProvider —— 纯合成、确定性、**零网络**。

用于全部单元测试与 CI。设计上刻意包含三类「脏数据」，把清洗流水线真正跑起来：

  * **跨组重复**：同一条 POI 同时出现在医疗组与购物教育组的召回结果里（真实场景中
    关键词会互相命中）-> 被 L2 精确去重掉；
  * **坐标漂移**：同 uid 出现两次、坐标相差 150~250 m（百度 POI 常见）-> 被 L4 校正；
  * **类型错标**：`社区药房` 被打上「诊所」tag、`张江生鲜超市` 被打上「菜市场」tag
    -> 被 L5 双证据规则纠正。

另外刻意构造一条**东西向河道（仅一座人行桥）**，使河道北侧的西段成为真实可达性盲区
（1 km 内无菜市场 / 药店 / 小学），保证 FR-4 有料可展示。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..constants import KEYWORD_TO_CATEGORY
from .barriers import AnalyticModel, BarrierLine, zhangjiang_model

#: 样例社区中心（BD09）—— 任务书 FR-1 验收原文地址附近
DEFAULT_CENTER = (31.2045, 121.5990)


# --------------------------------------------------------------------------
# 合成社区布局：(类别, 名称, x 东向 / 米, y 北向 / 米, tag, 附加属性)
# --------------------------------------------------------------------------
@dataclass
class _F:
    category: str
    name: str
    x: float
    y: float
    tag: str
    address: str
    uid: str | None = None
    dup_group: tuple[str, ...] = ("A", "B")
    dup_dx: float = 0.0
    dup_dy: float = 0.0


_LAYOUT: list[_F] = [
    # 坐标 = 以社区中心为原点的局部 ENU（x 东向 / y 北向，米）。
    # 河道在 y = -250（中心南侧 250 m），人行桥在 x = -700；
    # 「过河去南岸」因此成为一类真实的可达性损失（正是盲区的物理来源）。
    # 15 分钟步行域约 1.86 km²，覆盖 x ∈ [-820, 930]、y ∈ [-450, 960]。
    #
    # ---- 医疗组 ----
    _F("clinic", "张江社区卫生服务中心", -320.0, 250.0, "医疗;综合医院", "上海市浦东新区张江镇科苑路100号", "MK01"),
    _F("pharmacy", "华氏大药房(张江店)", 240.0, 380.0, "医疗;药店", "上海市浦东新区张江镇科苑路88号", "MK02",
       dup_dx=180.0, dup_dy=-150.0),
    _F("pharmacy", "国大药房(海科路店)", -717.0, -150.0, "医疗;药店", "上海市浦东新区海科路1600号", "MK03"),
    _F("pharmacy", "雷允上药房(南岸店)", -617.0, 650.0, "医疗;药店", "上海市浦东新区张江镇南岸路2号", "MK04"),
    _F("pharmacy", "好药师大药房(东苑店)", 883.0, -50.0, "医疗;药店", "上海市浦东新区张江镇东苑路8号", "MK05"),
    _F("pharmacy", "北苑药房", 83.0, 950.0, "医疗;药店", "上海市浦东新区张江镇北苑路3号", "MK06"),
    # 类型错标：名称含「药房」但 tag 是「诊所」-> L5 双证据规则应纠正为 pharmacy
    _F("clinic", "社区药房", 60.0, -230.0, "医疗;诊所", "上海市浦东新区张江镇居委路3号", "MK07"),
    _F("elderly_care", "张江敬老院", -600.0, 180.0, "政府机构;福利机构", "上海市浦东新区张江镇敬老路1号", "MK08"),
    _F("elderly_care", "张江日间照料中心", 700.0, -600.0, "政府机构;福利机构", "上海市浦东新区张江镇丹桂路8号", "MK09"),
    _F("elderly_care", "北苑长者照护之家", 183.0, 850.0, "政府机构;福利机构", "上海市浦东新区张江镇北苑路18号", "MK10"),
    # 超范围记录：位于 15 分钟圈外 -> 只影响 cleaning_funnel，不进覆盖统计
    _F("pharmacy", "远方药店(外区)", 2000.0, 20.0, "医疗;药店", "上海市浦东新区外区路1号", "MK30"),

    # ---- 购物 / 教育 / 交通 / 绿地组 ----
    _F("market", "张江农贸市场", -500.0, -300.0, "购物;市场", "上海市浦东新区张江镇农贸路5号", "MK11"),
    _F("market", "东苑菜市场", 683.0, 650.0, "购物;市场", "上海市浦东新区张江镇东苑路12号", "MK12",
       dup_dx=210.0, dup_dy=-140.0),
    _F("market", "北苑生鲜市场", -517.0, 750.0, "购物;市场", "上海市浦东新区张江镇北苑路6号", "MK13"),
    _F("market", "居委路菜场", 83.0, -150.0, "购物;市场", "上海市浦东新区张江镇居委路20号", "MK14"),
    _F("market", "南岸农贸市场", 783.0, -150.0, "购物;市场", "上海市浦东新区张江镇南岸路11号", "MK15"),
    _F("supermarket", "联华超市(张江店)", -350.0, 280.0, "购物;超市", "上海市浦东新区张江镇科苑路66号", "MK16"),
    _F("supermarket", "全家便利店(海科路店)", 220.0, -240.0, "购物;便利店", "上海市浦东新区海科路1650号", "MK17"),
    _F("supermarket", "罗森便利店(科苑路店)", 420.0, 320.0, "购物;便利店", "上海市浦东新区科苑路1500号", "MK18"),
    _F("supermarket", "农工商超市(张江店)", 2650.0, -150.0, "购物;超市", "上海市浦东新区外区路9号", "MK31"),
    _F("primary_school", "张江镇中心小学", 150.0, -480.0, "教育培训;小学", "上海市浦东新区张江镇小学路1号", "MK19"),
    _F("primary_school", "东苑外国语小学", 883.0, 250.0, "教育培训;小学", "上海市浦东新区张江镇东苑路28号", "MK20"),
    _F("primary_school", "南岸第二小学", -617.0, 650.0, "教育培训;小学", "上海市浦东新区张江镇南岸路33号", "MK21"),
    # ⚠️ 刻意保留一处「小学覆盖缺口」：北苑一带到最近小学的步行距离 > 1000 m
    #    （河道 + 桥位造成的真实可达性损失），用于验证 FR-4 盲区识别链路。
    _F("primary_school", "北苑实验小学", 600.0, 700.0, "教育培训;小学", "上海市浦东新区张江镇北苑路20号", "MK22"),
    # 类型错标：名称含「超市」但 tag 是「市场」-> L5 应纠正为 supermarket
    _F("market", "张江生鲜超市", 240.0, 80.0, "购物;市场", "上海市浦东新区张江镇居委路12号", "MK23"),
    _F("bus_stop", "科苑路海科路站", -160.0, 90.0, "交通设施;公交车站", "", "MK24"),
    _F("bus_stop", "张江镇政府站", -460.0, 300.0, "交通设施;公交车站", "", "MK25"),
    _F("bus_stop", "创新桥站", -660.0, -120.0, "交通设施;公交车站", "", "MK26"),
    _F("park", "张江主题公园", -100.0, -480.0, "旅游景点;公园", "上海市浦东新区张江镇公园路1号", "MK27"),
    _F("park", "北苑绿地公园", 300.0, 700.0, "旅游景点;公园", "上海市浦东新区张江镇北苑路40号", "MK28"),
    _F("park", "居委小广场", 30.0, 150.0, "旅游景点;广场", "", "MK29"),
]

#: 需要从检索结果中剔除的脏数据（触发 L5 name_exclude 规则）
_JUNK = [
    {"uid": "JK01", "name": "张江建材市场", "tag": "购物;市场", "x": 600.0, "y": -900.0,
     "address": "上海市浦东新区张江镇建材路1号", "groups": ("B",)},
    {"uid": "JK02", "name": "阳光少儿英语培训中心", "tag": "教育培训;培训机构", "x": 380.0, "y": 520.0,
     "address": "上海市浦东新区张江镇培训路2号", "groups": ("B",)},
    {"uid": "JK03", "name": "张江家具建材城", "tag": "购物;市场", "x": -1100.0, "y": -1400.0,
     "address": "上海市浦东新区张江镇建材路9号", "groups": ("B",)},
    # 真实场景中「小学」检索词会带出幼儿园/托儿所 -> L5 规则应剔除
    {"uid": "JK04", "name": "张江小学附属幼儿园", "tag": "教育培训;幼儿园", "x": 200.0, "y": -300.0,
     "address": "上海市浦东新区张江镇幼教路3号", "groups": ("B",)},
]


def _proj_consts(lat0: float):
    r = 6371008.8
    m_per_deg_lat = math.radians(1.0) * r
    m_per_deg_lng = m_per_deg_lat * math.cos(math.radians(lat0))
    return m_per_deg_lat, m_per_deg_lng


def xy_to_latlng(center: tuple[float, float], x: float, y: float) -> tuple[float, float]:
    m_lat, m_lng = _proj_consts(center[0])
    return center[0] + y / m_lat, center[1] + x / m_lng


def latlng_to_xy(center: tuple[float, float], lat: float, lng: float) -> tuple[float, float]:
    m_lat, m_lng = _proj_consts(center[0])
    return (lng - center[1]) * m_lng, (lat - center[0]) * m_lat


# --------------------------------------------------------------------------
# MockRouteProvider
# --------------------------------------------------------------------------
@dataclass
class MockRouteProvider:
    """确定性合成算路。真值 = AnalyticModel（与盲区预筛共用同一实现）。"""

    center: tuple[float, float] = DEFAULT_CENTER
    model: AnalyticModel = field(default_factory=zhangjiang_model)
    name: str = "mock"
    noise_s: float = 0.0      # 可注入确定性噪声（按坐标哈希），用于测试鲁棒性
    dead_zone_radius_m: float = 0.0  # >0 时该半径内返回 None，模拟「不可达」

    def __post_init__(self) -> None:
        self.last_meta: dict = {"source": "mock", "degraded": False, "reasons": [], "requests": 0}
        self.calls: list[int] = []

    def walking_matrix(self, origin, destinations):
        self.last_meta["requests"] += 1
        self.calls.append(len(destinations))
        o = latlng_to_xy(self.center, origin[0], origin[1])
        out: list[float | None] = []
        for dest in destinations:
            d = latlng_to_xy(self.center, dest[0], dest[1])
            t = self.model.travel_time_s(o, d)
            if self.noise_s:
                h = (hash((round(dest[0], 6), round(dest[1], 6))) % 2000) / 1000.0 - 1.0
                t += self.noise_s * h
            if self.dead_zone_radius_m and math.hypot(d[0], d[1]) < self.dead_zone_radius_m:
                out.append(None)
                continue
            out.append(max(1.0, t))
        return out


# --------------------------------------------------------------------------
# MockPlaceProvider
# --------------------------------------------------------------------------
@dataclass
class MockPlaceProvider:
    """按关键字过滤合成 POI。返回百度原始 JSON 形态（`results`）。"""

    center: tuple[float, float] = DEFAULT_CENTER
    model: AnalyticModel = field(default_factory=zhangjiang_model)
    name: str = "mock"

    def __post_init__(self) -> None:
        self.last_meta: dict = {"source": "mock", "degraded": False, "reasons": [], "requests": 0}
        self.queries: list[str] = []

    def _raw_records(self) -> list[dict]:
        recs: list[dict] = []
        for f in _LAYOUT:
            lat, lng = xy_to_latlng(self.center, f.x, f.y)
            recs.append({
                "uid": f.uid, "name": f.name, "lat": lat, "lng": lng, "tag": f.tag,
                "address": f.address, "groups": f.dup_group,
            })
            if f.dup_dx or f.dup_dy:
                lat2, lng2 = xy_to_latlng(self.center, f.x + f.dup_dx, f.y + f.dup_dy)
                recs.append({
                    "uid": f.uid, "name": f.name, "lat": lat2, "lng": lng2, "tag": f.tag,
                    "address": "", "groups": f.dup_group,
                })
        for j in _JUNK:
            lat, lng = xy_to_latlng(self.center, j["x"], j["y"])
            recs.append({"uid": j["uid"], "name": j["name"], "lat": lat, "lng": lng,
                         "tag": j["tag"], "address": j["address"], "groups": j["groups"]})
        return recs

    def search(self, query: str, center: tuple[float, float], radius: int = 2000, page_num: int = 0) -> dict:
        self.last_meta["requests"] += 1
        self.queries.append(query)
        keywords = [k for k in query.split("$") if k]
        wanted = {KEYWORD_TO_CATEGORY.get(k) for k in keywords}
        wanted.discard(None)

        out = []
        for r in self._raw_records():
            # 用「名称或 tag 含任一关键字」模拟百度的召回行为（真实场景中关键词会互相命中，
            # 所以同一条 POI 常同时出现在医疗组与购物组的召回结果里 -> 需要 L2 去重）
            hit = any(k in (r["name"] or "") or k in (r["tag"] or "") for k in keywords)
            if not hit:
                continue
            out.append({
                "uid": r["uid"], "name": r["name"], "tag": r["tag"], "address": r["address"],
                "location": {"lat": r["lat"], "lng": r["lng"]},
                "detail_info": {"tag": r["tag"]},
            })
        if page_num > 0:
            out = []  # mock 只有一页
        return {"status": 0, "message": "ok", "results": out, "total": len(out)}