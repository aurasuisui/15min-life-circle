"""路径/POI 数据来源抽象（**早期版本，已被 app/providers/ 取代**）。

⚠️ 保留原因：`backend/tests/test_isochrone_multi.py` 与 `_superseded/` 仍引用本模块。
**新代码请一律使用 `app.providers`**（CONTRACT §6.1）：

    from app.providers import get_route_provider, get_place_provider

对比：

| | app/services/providers.py（旧） | app/providers/（新，S1 交付） |
| --- | --- | --- |
| 模式 | mock / seed / live | **mock / replay / live / hybrid** |
| 路线 | 只有批量矩阵 | **BatchMatrixProvider + DirectionLiteProvider 双路线，命中 211 自动降级** |
| 配额 | 内存计数 | **SQLite 账本，预留/提交/释放/熔断** |
| 缓存 | 无 | **参数哈希 + TTL，命中不计配额** |
| 限流 | 简单 sleep | **按服务分桶的令牌桶（2.5 QPS）** |

三种实现：
- MockRouteProvider   : 合成数据，用于单元测试（不联网）
- SeedRouteProvider   : 读取 data/seed 快照（评审方零配额跑通）
- LiveBaiduProvider   : 实时调用百度地图 Web 服务 API（需服务端 AK + SN 签名）
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# 百度地图硬性限制（文档实测确认）
MAX_WALKING_ROUTES_PER_REQUEST = 50  # 步行批量算路：起终点个数之积 <= 50
MAX_WALKING_ROUTE_DISTANCE_M = 200_000  # 步行起终点距离 > 200KM 报参数错误


class QuotaExhaustedError(RuntimeError):
    """当日配额耗尽 —— 必须快速失败，不得重试。"""


class TransientApiError(RuntimeError):
    """瞬时失败（限流/网络抖动）—— 应指数退避重试。"""


@dataclass
class QuotaLedger:
    """配额账本。批量算路的配额按【最终路线数】计，不是按请求数。"""

    daily_route_limit: int = 5000
    daily_poi_limit: int = 100
    routes_used: int = 0
    poi_used: int = 0
    requests_made: int = 0

    def charge_routes(self, n: int) -> None:
        if self.routes_used + n > self.daily_route_limit:
            raise QuotaExhaustedError(
                f"算路配额不足：已用 {self.routes_used}，本次需 {n}，上限 {self.daily_route_limit}"
            )
        self.routes_used += n
        self.requests_made += 1

    def charge_poi(self, n: int = 1) -> None:
        if self.poi_used + n > self.daily_poi_limit:
            raise QuotaExhaustedError(
                f"POI 检索配额不足：已用 {self.poi_used}，本次需 {n}，上限 {self.daily_poi_limit}"
            )
        self.poi_used += n

    def snapshot(self) -> dict:
        return {
            "routes_used": self.routes_used,
            "routes_limit": self.daily_route_limit,
            "poi_used": self.poi_used,
            "poi_limit": self.daily_poi_limit,
            "requests_made": self.requests_made,
        }


class RouteProvider(Protocol):
    def walking_matrix(
        self, origin: tuple[float, float], destinations: list[tuple[float, float]]
    ) -> list[float | None]:
        """返回每个目的地相对起点的步行耗时（秒）。无结果的位置为 None。"""
        ...


# --------------------------------------------------------------------------
# Mock：测试用，速度随方位各向异性变化，模拟真实路网的方向性差异
# --------------------------------------------------------------------------
@dataclass
class MockRouteProvider:
    """合成路径数据源，用于单元测试（不联网）。

    耗时对半径是仿射的：t = detour_factor * r / speed(bearing) + overhead_s
    因为线性插值对仿射函数是精确的，所以可以用它逐方位校验反解逻辑。
    """

    base_speed: float = 1.2       # m/s（成年人平均步行速度约 1.2~1.4）
    anisotropy: float = 0.3       # 方位间速度差异幅度
    overhead_s: float = 30.0      # 固定开销（红绿灯、过街、找入口）
    detour_factor: float = 1.25   # 真实路线比直线距离长的系数
    pref_bearing_deg: float = 45.0
    calls: list[int] = field(default_factory=list)

    def speed_at(self, bearing_deg: float) -> float:
        return self.base_speed + self.anisotropy * math.cos(
            math.radians(bearing_deg - self.pref_bearing_deg)
        )

    @staticmethod
    def _true_bearing(origin: tuple[float, float], dest: tuple[float, float]) -> float:
        """真实初始方位角。

        注意：不能用 atan2(dlat, dlng) —— 那是"度数空间"的夹角，
        由于经度一度的实际长度是纬度的 cos(lat) 倍，两者并不相等。
        """
        lat1, lat2 = math.radians(origin[0]), math.radians(dest[0])
        dlng = math.radians(dest[1] - origin[1])
        x = math.sin(dlng) * math.cos(lat2)
        y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlng)
        return math.degrees(math.atan2(x, y)) % 360

    def walking_matrix(self, origin, destinations):
        from .geo import haversine_m

        self.calls.append(len(destinations))
        out: list[float | None] = []
        for dest in destinations:
            d = haversine_m(origin, dest)
            speed = self.speed_at(self._true_bearing(origin, dest))
            out.append(self.detour_factor * d / speed + self.overhead_s)
        return out


# --------------------------------------------------------------------------
# Seed：读本地快照（离线回放）
# --------------------------------------------------------------------------
@dataclass
class SeedRouteProvider:
    seed_dir: Path

    def _key(self, origin, destinations) -> str:
        raw = json.dumps([origin, destinations], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def walking_matrix(self, origin, destinations):
        f = self.seed_dir / f"route_{self._key(origin, destinations)}.json"
        if not f.exists():
            raise QuotaExhaustedError(
                f"离线模式下缺少快照 {f.name}；请切换到 DATA_MODE=live 或补充 seed 数据"
            )
        return json.loads(f.read_text(encoding="utf-8"))["durations"]


# --------------------------------------------------------------------------
# Live：实时调用百度地图（服务端 AK + SN 签名）
# --------------------------------------------------------------------------
@dataclass
class LiveBaiduProvider:
    ak: str
    sk: str
    ledger: QuotaLedger
    qps_limit: float = 2.5
    max_retries: int = 3
    _last_call: float = 0.0

    WALKING_MATRIX_URL = "https://api.map.baidu.com/routematrix/v2/walking"
    PLACE_SEARCH_URL = "https://api.map.baidu.com/place/v2/search"
    GEOCODING_URL = "https://api.map.baidu.com/geocoding/v3/"

    # ---- SN 签名：AK 明文不出现在前端，签名在后端完成 ----
    def _sign(self, path: str, params: dict) -> str:
        raw = path + "?" + urllib.parse.urlencode(
            {k: params[k] for k in sorted(params)}
        ) + self.sk
        return hashlib.md5(urllib.parse.quote_plus(raw).encode()).hexdigest()

    def _throttle(self) -> None:
        """令牌桶简化实现：保证调用间隔 >= 1/qps_limit。"""
        min_interval = 1.0 / max(self.qps_limit, 0.1)
        elapsed = time.monotonic() - self._last_call
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_call = time.monotonic()

    def _get(self, url: str, params: dict) -> dict:
        import httpx

        params = dict(params)
        params["ak"] = self.ak
        if self.sk:
            path = urllib.parse.urlparse(url).path
            params["timestamp"] = str(int(time.time()))
            params["sn"] = self._sign(path, params)

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                r = httpx.get(url, params=params, timeout=20.0)
                r.raise_for_status()
                data = r.json()
            except Exception as e:  # 网络类异常 —— 可重试
                last_err = TransientApiError(str(e))
                time.sleep(2 ** attempt)
                continue

            status = data.get("status")
            if status == 0:
                return data
            # 百度状态码：2 参数/AK 权限错误；4/5 配额或权限问题
            if status in (2, 4, 5):
                raise QuotaExhaustedError(f"百度接口拒绝（status={status}）：{data.get('message')}")
            last_err = TransientApiError(f"status={status} {data.get('message')}")
            time.sleep(2 ** attempt)
        raise last_err or TransientApiError("未知错误")

    def walking_matrix(self, origin, destinations):
        results: list[float | None] = []
        # 按 MAX_WALKING_ROUTES_PER_REQUEST 分块（1 起点 × N 终点）
        for i in range(0, len(destinations), MAX_WALKING_ROUTES_PER_REQUEST):
            chunk = destinations[i : i + MAX_WALKING_ROUTES_PER_REQUEST]
            self.ledger.charge_routes(len(chunk))  # 按路线数计费，不是请求数
            data = self._get(
                self.WALKING_MATRIX_URL,
                {
                    "origins": f"{origin[0]},{origin[1]}",
                    "destinations": "|".join(f"{d[0]},{d[1]}" for d in chunk),
                    "output": "json",
                    "coord_type": "bd09ll",
                },
            )
            for item in data.get("result", []):
                dur = (item or {}).get("duration") or {}
                v = dur.get("value")
                results.append(float(v) if v else None)
        return results

    def place_search(self, query: str, center, radius: int = 2000, page_num: int = 0) -> dict:
        self.ledger.charge_poi(1)
        return self._get(
            self.PLACE_SEARCH_URL,
            {
                "query": query,
                "location": f"{center[0]},{center[1]}",
                "radius": radius,
                "radius_limit": "true",  # 关键：防止半径越界后退化为全市检索
                "page_size": 20,
                "page_num": page_num,
                "scope": 2,
                "output": "json",
            },
        )

    def geocode(self, address: str, city: str = "上海") -> tuple[float, float] | None:
        data = self._get(
            self.GEOCODING_URL,
            {"address": address, "city": city, "output": "json", "ret_coordtype": "bd09ll"},
        )
        loc = data.get("result", {}).get("location")
        return (loc["lat"], loc["lng"]) if loc else None
