"""Live Provider —— 真实调用百度地图 Web 服务 API。

⚠️ **双路线 provider**（本项目的硬性要求，见 CONTRACT §3）：

  * `BatchMatrixProvider`   —— 走 `/routematrix/v2/walking`，1 请求 50 条路线（主力）
  * `DirectionLiteProvider` —— 走 `/directionlite/v1/walking`，1 请求 1 条（兜底）
  * `FallbackMatrixRouteProvider` —— 先试批量矩阵；命中 211/240/服务不可用时**自动熔断并降级**
    到 directionlite，并把原因写进 `last_meta.reasons`，最终汇总到报告的 `meta.degraded`。

本机实测（2026-10-14）：routematrix 返回 `status=211 APP SN校验失败`，而同一个 AK 的
geocoding / place/v3 / directionlite 均正常 —— **先排除签名，再怀疑服务权限**。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..baidu.client import BaiduClient, EndpointDegraded, get_client
from ..baidu.errors import KIND_PERMISSION, KIND_SN, BaiduApiError, QuotaExhaustedError
from .base import SeedMissingError

_MAX_ROUTES_PER_REQUEST = 50


@dataclass
class LiveGeocodeProvider:
    client: BaiduClient = field(default_factory=get_client)
    city: str = "上海市"
    name: str = "live"

    def __post_init__(self) -> None:
        self.last_meta = {"source": "live", "degraded": False, "reasons": []}

    def geocode(self, address: str) -> tuple[float, float] | None:
        return self.client.geocode(address, city=self.city)


@dataclass
class LivePlaceProvider:
    client: BaiduClient = field(default_factory=get_client)
    name: str = "live"

    def __post_init__(self) -> None:
        self.last_meta = {"source": "live", "degraded": False, "reasons": []}

    def search(self, query: str, center, radius: int = 2000, page_num: int = 0) -> dict:
        return self.client.place_around(query, center, radius=radius, page_num=page_num)


# --------------------------------------------------------------------------
# 路线：批量矩阵（主力）
# --------------------------------------------------------------------------
@dataclass
class BatchMatrixProvider:
    """`/routematrix/v2/walking` —— 1 请求 50 条路线，配额按【路线数】计。"""

    client: BaiduClient = field(default_factory=get_client)
    name: str = "routematrix"

    def __post_init__(self) -> None:
        self.last_meta = {"source": "live:routematrix", "degraded": False, "reasons": []}

    def walking_matrix(self, origin, destinations):
        durations: list[float | None] = []
        for i in range(0, len(destinations), _MAX_ROUTES_PER_REQUEST):
            chunk = destinations[i : i + _MAX_ROUTES_PER_REQUEST]
            durs, _dists = self.client.walking_matrix(origin, chunk)
            durations.extend(durs)
        if len(durations) < len(destinations):
            durations.extend([None] * (len(destinations) - len(durations)))
        return durations


# --------------------------------------------------------------------------
# 路线：轻量单条（兜底）
# --------------------------------------------------------------------------
@dataclass
class DirectionLiteProvider:
    """`/directionlite/v1/walking` —— 1 请求 1 条路线。

    配额按请求数计，所以同样 396 条路线会消耗 396 次请求（批量只要 9 次）。
    这就是为什么必须保留 batch 作为主力、只在服务不可用时降级。
    """

    client: BaiduClient = field(default_factory=get_client)
    name: str = "directionlite"
    sleeper: object = time.sleep

    def __post_init__(self) -> None:
        self.last_meta = {"source": "live:directionlite", "degraded": False, "reasons": []}
        self.request_count = 0

    def walking_matrix(self, origin, destinations):
        out: list[float | None] = []
        failed = 0
        for dest in destinations:
            self.request_count += 1
            try:
                t, _d = self.client.walking_route(origin, dest)
            except QuotaExhaustedError:
                raise
            except BaiduApiError as e:
                # 单点失败不拖垮整批（SOLUTION §5.2「参数错误」一行）
                failed += 1
                if failed <= 3:
                    self.last_meta["reasons"].append(f"directionlite 单点失败：{e.message}")
                out.append(None)
                continue
            out.append(t)
        if failed:
            self.last_meta["degraded"] = True
        return out


# --------------------------------------------------------------------------
# 双路线：自动降级
# --------------------------------------------------------------------------
class FallbackMatrixRouteProvider:
    """先批量矩阵，不可用时自动切 directionlite，并把降级原因记录下来。"""

    def __init__(self, matrix=None, lite=None):
        self.matrix = matrix if matrix is not None else BatchMatrixProvider()
        self.lite = lite if lite is not None else DirectionLiteProvider()
        self.last_meta: dict = {"source": "live:routematrix", "degraded": False, "reasons": []}
        self._use_lite = False
        self._probe_done = False

    def _record(self, reason: str) -> None:
        if reason not in self.last_meta["reasons"]:
            self.last_meta["reasons"].append(reason)
        self.last_meta["degraded"] = True

    def walking_matrix(self, origin, destinations):
        if self._use_lite:
            self.last_meta["source"] = "live:directionlite"
            return self.lite.walking_matrix(origin, destinations)
        try:
            return self.matrix.walking_matrix(origin, destinations)
        except (EndpointDegraded, QuotaExhaustedError):
            raise
        except BaiduApiError as e:
            if e.kind not in (KIND_SN, KIND_PERMISSION):
                raise
            # 211/240：批量算路服务不可用 -> 熔断并降级
            self._use_lite = True
            self.last_meta["source"] = "live:directionlite"
            self._record(f"批量算路（routematrix）不可用：status={e.status} {e.message}；已自动降级到 directionlite")
            return self.lite.walking_matrix(origin, destinations)


# --------------------------------------------------------------------------
# 在线 = 快照未命中时回退到 live
# --------------------------------------------------------------------------
class OnlineFirstRouteProvider:
    """先查本地快照（零配额），未命中再走 live。用于 `hybrid` 模式。

    `allow_analytic=True` 时，仍然缺数据就退到解析模型（`t ≈ 路网长度 / v + 过街惩罚`），
    并标注 degraded —— 这对应 SOLUTION §5.3 降级链的最后一环。**绝不静默**。
    """

    def __init__(self, seed, live, analytic=None, allow_analytic: bool = True, center=None):
        self.seed = seed
        self.live = live
        self.analytic = analytic
        self.allow_analytic = allow_analytic
        self.center = center
        self.last_meta: dict = {"source": "hybrid", "degraded": False, "reasons": []}
        self.snapshot_hits = 0
        self.live_hits = 0
        self.analytic_hits = 0

    def _note(self, reason: str) -> None:
        if reason not in self.last_meta["reasons"]:
            self.last_meta["reasons"].append(reason)
        self.last_meta["degraded"] = True

    def walking_matrix(self, origin, destinations):
        try:
            out = self.seed.walking_matrix(origin, destinations)
            self.snapshot_hits += 1
            return out
        except SeedMissingError:
            pass
        try:
            out = self.live.walking_matrix(origin, destinations)
            self.live_hits += 1
            self.last_meta["source"] = "hybrid:live"
            return out
        except QuotaExhaustedError as e:
            self._note(f"算路配额不足，改用本地解析模型：{e.message}")
        if not self.allow_analytic or self.analytic is None:
            raise QuotaExhaustedError("route", -1, "hybrid 模式下算路快照缺失且配额不可用")

        from .mock import latlng_to_xy  # 复用同一套局部投影（避免正负号不一致）

        center = self.center or origin
        o = latlng_to_xy(center, origin[0], origin[1])
        self.analytic_hits += 1
        return [self.analytic.travel_time_s(o, latlng_to_xy(center, d[0], d[1])) for d in destinations]
