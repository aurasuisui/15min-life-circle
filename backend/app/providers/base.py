"""Provider 协议（CONTRACT.md §6.1）。

约定：
  * 两个 Protocol 的方法都是**同步**的（核心算法 `build_isochrones_multi` 是同步的，
    API 层用 `asyncio.to_thread` 把整条链路放到线程池，不阻塞事件循环）；
  * 所有 provider 都在实例上挂 `last_meta = {"source", "degraded", "reasons"}`，
    供 `meta.degraded / meta.degrade_reasons` 汇总；
  * 任何 provider 都不允许「静默」——降级必须留下 reasons。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..baidu.errors import QuotaExhaustedError  # noqa: F401  (对外统一出口)


class ProviderError(RuntimeError):
    """provider 层错误基类。"""


class SeedMissingError(ProviderError):
    """快照缺失：离线回放时找不到对应记录。"""


@runtime_checkable
class RouteProvider(Protocol):
    def walking_matrix(
        self, origin: tuple[float, float], destinations: list[tuple[float, float]]
    ) -> list[float | None]:
        """返回每个目的地相对起点的步行耗时（秒）。无结果的位置为 None。"""
        ...


@runtime_checkable
class PlaceProvider(Protocol):
    def search(
        self, query: str, center: tuple[float, float], radius: int = 2000, page_num: int = 0
    ) -> dict:
        """返回百度原始 JSON 形态的检索结果（`results` + `status`）。"""
        ...


def meta_of(provider: Any) -> dict:
    """安全读取 provider 的元信息。"""
    m = getattr(provider, "last_meta", None)
    if not isinstance(m, dict):
        return {"source": getattr(provider, "name", "unknown"), "degraded": False, "reasons": []}
    return m


class RecordingRouteProvider:
    """装饰器：包住任意 RouteProvider，记录每一次 (origin, destinations) 调用。

    用途：
      1. `scripts/collect_seed.py` 据此生成 replay 所需的快照文件；
      2. 审计流程据此统计真实请求数（bench 耗时对比数据）。
    """

    def __init__(self, inner: RouteProvider, record: bool = True):
        self.inner = inner
        self.record = record
        self.requests: list[dict] = []
        self.responses: list[list] = []   # 与 requests 一一对应的原始耗时（避免采集时二次调用真实 API）
        self.destination_count = 0

    @property
    def last_meta(self) -> dict:
        return meta_of(self.inner)

    @property
    def name(self) -> str:
        return getattr(self.inner, "name", "wrapped")

    def walking_matrix(self, origin, destinations):
        out = self.inner.walking_matrix(origin, destinations)
        if self.record:
            self.requests.append({
                "origin": [float(origin[0]), float(origin[1])],
                "destinations": [[float(a), float(b)] for a, b in destinations],
            })
            self.responses.append(list(out))
            self.destination_count += len(destinations)
        return out
