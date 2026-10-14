"""BaiduClient —— SN 签名 + 令牌桶 + 配额账本 + 文件缓存 + 错误分类。

一次请求的完整链路（顺序即优先级）：

    缓存命中?  -> 直接返回（**不计配额、不占令牌**）
      否
    账本 reserve(n)  -> 配额不足则【快速失败】QuotaExhaustedError
      否
    令牌桶 acquire() -> 按服务分桶限速 2.5 QPS
      否
    SN 签名 -> HTTP GET（超时 20s，香港出口延迟偏高）
      否
    错误分类(service, status, message)
      ├ quota      -> mark_exhausted + 快速失败（不重试）
      ├ qps/瞬时   -> 指数退避 + 全抖动，最多 max_retries 次
      └ ak/sn/权限 -> 熔断该服务并抛错（重试是白烧配额）
      ↓
    commit(n) 成功计费 + 写缓存
"""
from __future__ import annotations

import logging
import random
import time
import urllib.parse
from typing import Any

import httpx

from ..config import settings
from .cache import FileCache, cache_key
from .errors import (
    KIND_PERMISSION,
    KIND_SN,
    BaiduApiError,
    QuotaExhaustedError,
    TransientApiError,
    classify,
    to_exception,
)
from .quota import QuotaLedger
from .ratelimit import SERVICE_GEOCONV, SERVICE_GEOCODE, SERVICE_PLACE, SERVICE_ROUTE, get_bucket
from .sign import bd_encode, make_sn

log = logging.getLogger(__name__)

BASE_URL = "https://api.map.baidu.com"

#: 端点 -> (path, 服务名, 默认配额单位)
ENDPOINTS = {
    "geocode": ("/geocoding/v3/", SERVICE_GEOCODE),
    "geoconv": ("/geoconv/v1/", SERVICE_GEOCONV),
    "place": ("/place/v3/around", SERVICE_PLACE),
    "routematrix": ("/routematrix/v2/walking", SERVICE_ROUTE),
    "directionlite": ("/directionlite/v1/walking", SERVICE_ROUTE),
}

#: 已知不可用的端点（本机实测 routematrix 返回 211：SN 校验失败【或】服务未开通）。
#: 命中后自动降级到 directionlite，并在 meta.degraded 里标注。
_UNAVAILABLE: set[str] = set()


class EndpointDegraded(RuntimeError):
    """端点已被判定不可用（例如 routematrix 211），本次直接走降级路径。"""

    def __init__(self, endpoint: str, reason: str):
        self.endpoint = endpoint
        self.reason = reason
        super().__init__(f"[{endpoint}] 已降级：{reason}")


def endpoint_available(name: str) -> bool:
    return name not in _UNAVAILABLE


def mark_unavailable(name: str) -> None:
    _UNAVAILABLE.add(name)


def reset_endpoints() -> None:
    _UNAVAILABLE.clear()


class BaiduClient:
    """百度地图 Web 服务 API 客户端。线程安全（令牌桶与账本各自带锁）。"""

    def __init__(
        self,
        ak: str | None = None,
        sk: str | None = None,
        ledger: QuotaLedger | None = None,
        cache: FileCache | None = None,
        qps: float | None = None,
        timeout_s: float = 20.0,
        max_retries: int = 3,
        http_client: Any = None,
    ):
        self.ak = settings.baidu_map_server_ak if ak is None else ak
        self.sk = settings.baidu_map_server_sk if sk is None else sk
        self.qps = float(settings.api_qps_limit if qps is None else qps)
        self.timeout_s = max(20.0, float(timeout_s))
        self.max_retries = int(max_retries)
        self.ledger = ledger if ledger is not None else QuotaLedger(db_path=str(_default_db()))
        self.cache = cache if cache is not None else FileCache(settings.cache_dir)
        self._client = http_client
        self.calls: dict[str, int] = {}
        self.degrade_reasons: list[str] = []

    # ------------------------------------------------------------------ HTTP
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(self.timeout_s, connect=min(10.0, self.timeout_s)),
                headers={"User-Agent": "15min-life-circle/0.1 (+https://github.com/)"},
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # ------------------------------------------------------------- 核心请求
    def get(
        self,
        endpoint: str,
        params: dict,
        *,
        charge_units: int | None = None,
        ttl_s: float | None = None,
        use_cache: bool = True,
    ) -> dict:
        """统一入口。返回百度原始 JSON（status=0）。失败按分类抛对应异常。"""
        path, service = ENDPOINTS[endpoint]
        if endpoint in _UNAVAILABLE:
            raise EndpointDegraded(endpoint, "该端点此前已被判定不可用")

        units = int(charge_units if charge_units is not None else 1)
        key = cache_key(f"{service}:{endpoint}", params)
        if use_cache:
            cached = self.cache.get(key)
            if cached is not None:
                self.calls["cache_hit"] = self.calls.get("cache_hit", 0) + 1
                return cached

        # 预占配额：不足 -> 快速失败，绝不重试
        self.ledger.reserve(service, units)
        try:
            data = self._get_with_retries(endpoint, path, service, params)
        except Exception:
            self.ledger.release(service, units)
            raise
        self.ledger.commit(service, units)

        self.calls[endpoint] = self.calls.get(endpoint, 0) + 1
        if use_cache:
            self.cache.set(key, service, data, ttl_s)
        return data

    def _get_with_retries(self, endpoint: str, path: str, service: str, params: dict) -> dict:
        bucket = get_bucket(service, rate=self.qps, burst=3)
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            bucket.acquire()
            try:
                raw = self._signed_get(path, params)
            except httpx.HTTPStatusError as e:
                last = TransientApiError(service, e.response.status_code, f"HTTP {e.response.status_code}")
            except Exception as e:  # 网络类异常（超时/连接重置/DNS）
                last = TransientApiError(service, -1, f"{type(e).__name__}: {e}")
            else:
                status = int(raw.get("status", -1))
                if status == 0:
                    return raw
                message = str(raw.get("message", ""))
                ec = classify(service, status, message)
                assert ec is not None
                if ec.kind == "quota":
                    self.ledger.mark_exhausted(service)
                if ec.kind in (KIND_SN, KIND_PERMISSION):
                    # 本机实测：routematrix 在服务未开通时返回 211。同 AK 其他接口正常
                    # 而只有本端点 211 -> 是服务权限问题，不是签名问题。
                    mark_unavailable(endpoint)
                    self.degrade_reasons.append(f"{endpoint} 返回 {status}（{message}），已熔断并降级")
                last = to_exception(service, status, message)

            if not getattr(last, "retryable", False) or attempt >= self.max_retries:
                raise last
            # 指数退避 + 全抖动
            delay = min(8.0, 0.5 * (2 ** attempt)) * (0.5 + random.random())
            log.warning("百度 %s 第 %d 次失败（%s），%.2fs 后重试", endpoint, attempt + 1, last, delay)
            time.sleep(delay)
        raise last or TransientApiError(service, -1, "未知错误")

    def _signed_get(self, path: str, params: dict) -> dict:
        """构造 URL（参数顺序 = 签名顺序 = URL 顺序）并发起 GET。

        ⚠️ 参数顺序：必须是 URL 中出现的顺序；`ak` 必须在 params 里；
        `sn` 最后计算、最后拼接；使用 sn 时 `timestamp` 必填。
        """
        q: dict[str, Any] = dict(params)
        q.setdefault("output", "json")
        q["ak"] = self.ak
        if self.sk:
            q["timestamp"] = str(int(time.time()))
            q["sn"] = make_sn(path, q, self.sk)
        qs = "&".join(f"{k}={bd_encode(v)}" for k, v in q.items())
        resp = self._http().get(f"{BASE_URL}{path}?{qs}")
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------ 三个方法
    def geocode(self, address: str, city: str = "上海市") -> tuple[float, float] | None:
        """地址 -> BD09 坐标。配额服务：geocode（5000/日）。"""
        data = self.get("geocode", {
            "address": address, "city": city, "ret_coordtype": "bd09ll",
        })
        loc = (data.get("result") or {}).get("location")
        if not loc:
            return None
        return float(loc["lat"]), float(loc["lng"])

    def geoconv(self, lng: float, lat: float, from_type: int = 1, to_type: int = 5) -> tuple[float, float] | None:
        """坐标转换。1=WGS84, 3=GCJ02, 5=BD09LL。配额服务：geoconv。"""
        data = self.get("geoconv", {"coords": f"{lng},{lat}", "from": from_type, "to": to_type})
        res = data.get("result") or []
        if not res:
            return None
        return float(res[0]["y"]), float(res[0]["x"])

    def place_around(
        self,
        query: str,
        center: tuple[float, float],
        radius: int = 1500,
        page_num: int = 0,
        page_size: int = 20,
        scope: int = 2,
    ) -> dict:
        """地点检索（多关键字并集，`$` 分隔，最多 10 个）。配额服务：place（100/日，**按请求计费**）。

        必须传 radius_limit=true —— 否则半径超出城市边界会退化为「全市检索」。
        """
        return self.get("place", {
            "query": query,
            "location": f"{center[0]},{center[1]}",
            "radius": int(radius),
            "radius_limit": "true",
            "page_size": int(min(20, page_size)),
            "page_num": int(page_num),
            "scope": int(scope),
            "coord_type": 3,  # 3 = bd09ll
        })

    def walking_matrix(
        self,
        origin: tuple[float, float],
        destinations: list[tuple[float, float]],
        ret_straight_dist: bool = True,
        road_prefer: int | None = 3,
    ) -> tuple[list[float | None], list[float | None]]:
        """步行批量算路（1 起点 × N 终点，N ≤ 50）。返回 (耗时秒, 直线距离米)。

        ⚠️ 配额按【最终路线数】计，不是按请求数，所以 charge_units = len(destinations)。
        ⚠️ duration.value == 0 表示【不可达】，绝不能当作「耗时 0」。
        ⚠️ 本机实测该接口返回 211（服务未开通），命中后自动熔断并在上层降级。
        """
        if len(destinations) > 50:
            raise ValueError("步行批量算路单次最多 50 条路线")
        params: dict[str, Any] = {
            "origins": f"{origin[0]},{origin[1]}",
            "destinations": "|".join(f"{d[0]},{d[1]}" for d in destinations),
            "coord_type": "bd09ll",
        }
        if ret_straight_dist:
            params["ret_straight_dist"] = 1
        if road_prefer is not None:
            params["road_prefer"] = road_prefer
        data = self.get("routematrix", params, charge_units=len(destinations))
        durs: list[float | None] = []
        dists: list[float | None] = []
        for item in data.get("result") or []:
            item = item or {}
            dv = (item.get("duration") or {}).get("value")
            sv = (item.get("distance") or {}).get("value")
            durs.append(float(dv) if dv else None)
            dists.append(float(sv) if sv else None)
        while len(durs) < len(destinations):
            durs.append(None)
            dists.append(None)
        return durs, dists

    def walking_route(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
    ) -> tuple[float | None, float | None]:
        """轻量步行路线规划（1 请求 1 条）。routematrix 不可用时的降级路径。

        ⚠️ 这里 README 口径与 routematrix 不同：directionlite 单请求 1 条路线，
        配额按【请求数】计，所以 charge_units = 1。
        """
        data = self.get("directionlite", {
            "origin": f"{origin[0]},{origin[1]}",
            "destination": f"{destination[0]},{destination[1]}",
            "coord_type": "bd09ll",
        }, charge_units=1)
        routes = (data.get("result") or {}).get("routes") or []
        if not routes:
            return None, None
        r0 = routes[0]
        dur = r0.get("duration")
        dist = r0.get("distance")
        t = float(dur) if dur else None
        d = float(dist) if dist else None
        return t, d

    # -------------------------------------------------------------- 元信息
    def quota_snapshot(self) -> dict:
        return self.ledger.snapshot()

    def stats(self) -> dict:
        return {
            "calls": dict(self.calls),
            "cache": self.cache.stats(),
            "quota": self.ledger.snapshot(),
            "degrade_reasons": list(self.degrade_reasons),
        }


def _default_db():
    from pathlib import Path

    root = Path(settings.cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root / "quota.db"


_DEFAULT: BaiduClient | None = None


def get_client(**kwargs) -> BaiduClient:
    """进程级单例（账本与缓存必须共享，否则配额会被重复计算）。"""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = BaiduClient(**kwargs)
    return _DEFAULT
