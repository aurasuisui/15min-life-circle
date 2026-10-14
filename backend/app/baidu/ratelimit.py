"""按服务分桶的令牌桶限流（官方 3 QPS，本实现取 2.5，留 17% 余量）。

为什么按服务分桶：百度对「地点检索 / 算路 / 地理编码 / 坐标转换」分别计 QPS，
一个全局桶会让慢服务拖累快服务。

实现为【进程级共享桶】—— 多个 BaiduClient 实例（例如 live provider 与
directionlite provider）共用同一个服务的桶，否则并发时会双双突破 QPS。
"""
from __future__ import annotations

import threading
import time

_BUCKETS: dict[str, "TokenBucket"] = {}
_REGISTRY_LOCK = threading.Lock()

#: 服务名 -> 桶（与百度控制台的服务划分一致）
SERVICE_PLACE = "place"
SERVICE_ROUTE = "route"
SERVICE_GEOCODE = "geocode"
SERVICE_GEOCONV = "geoconv"


class TokenBucket:
    """线程安全令牌桶：容量 burst，按 rate 个/秒匀速补充，排队而非拒绝。"""

    def __init__(self, rate: float = 2.5, burst: int = 3, name: str = "default"):
        self.rate = float(rate)
        self.burst = int(burst)
        self.name = name
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self.acquired = 0

    def acquire(self, n: int = 1, timeout: float = 30.0) -> float:
        """取 n 个令牌，不足则阻塞等待。返回实际等待秒数（>timeout 抛 TimeoutError）。"""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    self.acquired += n
                    return waited
                need = n - self._tokens
                sleep_s = need / self.rate
            if waited + sleep_s > timeout:
                raise TimeoutError(f"令牌桶 {self.name} 等待超时（{timeout}s）")
            time.sleep(min(sleep_s, 0.25))
            waited += sleep_s


def get_bucket(name: str, rate: float = 2.5, burst: int = 3) -> TokenBucket:
    """取得（或创建）进程级共享桶。"""
    with _REGISTRY_LOCK:
        b = _BUCKETS.get(name)
        if b is None:
            b = TokenBucket(rate=rate, burst=burst, name=name)
            _BUCKETS[name] = b
        return b


def reset_buckets() -> None:
    """测试用：清空注册表。"""
    with _REGISTRY_LOCK:
        _BUCKETS.clear()
