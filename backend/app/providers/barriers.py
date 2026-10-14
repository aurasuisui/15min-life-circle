"""步行屏障（水体 / 铁路 / 封闭小区 / 快速路）与解析耗时模型。

**这一层是「盲区预筛」与「mock / 快照生成」共用的真值模型**，必须只有一份实现。
我们第一版就是两处各写一遍，结果预筛认为某处「750 s 可达」、真实算路却是 1590 s，
边界带完全选错、真值算路白花 —— 现在统一到 `AnalyticModel`。

解析耗时模型（SOLUTION §5.3）：

    t(x_f, x_p) = len(x_f, x_p) / v(bearing) + t_overhead
    v(bearing)  = v0 · (1 + a·cos(bearing − φ))        各向异性步行速度
    len(...)    = 直线距离 × 绕行系数
                  **跨屏障时替换为「经最近桥」的折线长度**（这是盲区的物理来源）

⚠️ 生产环境里 barriers 来自每个社区配置的屏障图层（水体/铁路/封闭小区/快速路），
一期人工勾绘、二期接百度水系与绿地分类 POI。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class BarrierLine:
    """一条不可穿越的横向带状屏障（简化表达：法向为 y 的直线水体/铁路）。

    y_m: 屏障中心线的局部 ENU y 坐标（米）
    half_width_m: 半宽（米）
    bridge_x_m: 跨越屏障的通道（桥 / 天桥 / 地道）的局部 x 坐标（米）
    name: 屏障名（写进报告与降级说明）
    """

    y_m: float
    half_width_m: float = 45.0
    bridge_x_m: float = -700.0
    name: str = "河道"

    def side(self, y: float) -> int:
        """点位于屏障的哪一侧：+1 / -1 / 0（位于带内）。"""
        if y > self.y_m + self.half_width_m:
            return 1
        if y < self.y_m - self.half_width_m:
            return -1
        return 0

    def crossing_length(self, a: tuple[float, float], b: tuple[float, float]) -> float | None:
        """a、b 分处两侧时返回经最近桥的折线长度；否则返回 None。"""
        sa, sb = self.side(a[1]), self.side(b[1])
        if sa == 0 or sb == 0 or sa == sb:
            return None
        bx = self.bridge_x_m
        return math.hypot(a[0] - bx, a[1] - self.y_m) + math.hypot(bx - b[0], self.y_m - b[1])


@dataclass
class AnalyticModel:
    """解析耗时模型。mock 与盲区预筛共用同一实例，保证「预筛 ↔ 真值」一致。"""

    base_speed: float = 1.25          # m/s（4.5 km/h），与 app/core/isochrone.py 的 walk_speed 同口径
    anisotropy: float = 0.15          # 各向异性幅度（方位间速度差）
    pref_bearing_deg: float = 30.0    # 最快方位
    detour_factor: float = 1.3        # 直线 -> 路网绕行系数（文献 1.2~1.5，取中值）
    overhead_s: float = 20.0          # 固定开销（红绿灯、过街、找入口）

    def effective_speed(self) -> float:
        """含绕行的等效步行速度（m/s）。`v_eff = v / detour_factor`。"""
        return self.base_speed / max(1e-6, self.detour_factor)
    barriers: list[BarrierLine] = field(default_factory=list)

    def speed_at(self, bearing_deg: float) -> float:
        return self.base_speed * (1.0 + self.anisotropy * math.cos(math.radians(bearing_deg - self.pref_bearing_deg)))

    def route_length_m(self, origin_xy: tuple[float, float], dest_xy: tuple[float, float]) -> float:
        straight = math.hypot(dest_xy[0] - origin_xy[0], dest_xy[1] - origin_xy[1])
        for bar in self.barriers:
            cross = bar.crossing_length(origin_xy, dest_xy)
            if cross is not None:
                return cross
        return straight * self.detour_factor

    def travel_time_s(self, origin_xy: tuple[float, float], dest_xy: tuple[float, float]) -> float:
        dx = dest_xy[0] - origin_xy[0]
        dy = dest_xy[1] - origin_xy[1]
        bearing = math.degrees(math.atan2(dx, dy)) % 360.0
        return self.route_length_m(origin_xy, dest_xy) / self.speed_at(bearing) + self.overhead_s

    def as_dict(self) -> dict:
        return {
            "base_speed": self.base_speed,
            "anisotropy": self.anisotropy,
            "pref_bearing_deg": self.pref_bearing_deg,
            "detour_factor": self.detour_factor,
            "overhead_s": self.overhead_s,
            "barriers": [
                {"name": b.name, "y_m": b.y_m, "half_width_m": b.half_width_m, "bridge_x_m": b.bridge_x_m}
                for b in self.barriers
            ],
        }


#: 张江海科路样例社区的默认屏障：一条东西向河道（创新河）+ 一座人行桥。
#: 河道位于中心点南侧 250 m，使「过河去南岸」成为一类真实的可达性损失。
def zhangjiang_model() -> AnalyticModel:
    return AnalyticModel(
        barriers=[BarrierLine(y_m=-250.0, half_width_m=45.0, bridge_x_m=-700.0, name="创新河")]
    )
