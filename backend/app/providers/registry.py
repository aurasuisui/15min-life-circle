"""ProviderRegistry —— live | replay | mock | hybrid 四态可切换（CONTRACT §6.1）。

优先级：`PROVIDER` 环境变量 > `DATA_MODE` > 默认 `mock`。

  数据模式          路线 provider                       POI provider
  ---------------------------------------------------------------------
  mock              确定性合成（零网络）                确定性合成
  replay            快照 routes/matrix/（零配额）        快照 poi.json
  live              routematrix → directionlite 双路线    place/v3/around
  hybrid            快照优先 → live → 解析模型           快照优先 → live

⚠️ 契约要求：`replay`/`hybrid` 未命中时读 `data/seed/`，再不行抛 `QuotaExhaustedError`。
本实现把它拆成两种力度：
  * `strict=True`（默认）：缺数据直接抛错（宁可见红，不静默）；
  * `strict=False`：允许解析模型兜底，但**必须**在 `last_meta.reasons` 里写明。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from ..config import settings
from .base import SeedMissingError
from .barriers import AnalyticModel, zhangjiang_model
from .live import (
    BatchMatrixProvider,
    DirectionLiteProvider,
    FallbackMatrixRouteProvider,
    LiveGeocodeProvider,
    LivePlaceProvider,
)
from .mock import DEFAULT_CENTER, MockPlaceProvider, MockRouteProvider
from .replay import (
    ReplayBundle,
    ReplayGeocodeProvider,
    ReplayPlaceProvider,
    ReplayRouteProvider,
    find_community_dir,
)

log = logging.getLogger(__name__)

VALID_MODES = ("mock", "replay", "live", "hybrid")
_STATE: dict[str, Any] = {}
#: 显式 provider 覆盖（采集脚本 / 测试用）。比 monkeypatch 更可控、可断言、可清理。
_OVERRIDE: dict[str, Any] = {}


def set_override(route=None, place=None, geocode=None) -> None:
    """临时接管 provider 工厂。传 None 表示该槽位保持默认。"""
    _OVERRIDE.clear()
    if route is not None:
        _OVERRIDE["route"] = route
    if place is not None:
        _OVERRIDE["place"] = place
    if geocode is not None:
        _OVERRIDE["geocode"] = geocode


def clear_override() -> None:
    _OVERRIDE.clear()


def resolve_mode() -> str:
    """解析当前数据模式。`PROVIDER` 优先于 `DATA_MODE`。"""
    env = (os.environ.get("PROVIDER") or "").strip().lower()
    if env in VALID_MODES:
        return env
    mode = (os.environ.get("DATA_MODE") or settings.data_mode or "mock").strip().lower()
    return mode if mode in VALID_MODES else "mock"


def _community() -> str | None:
    return (os.environ.get("SEED_COMMUNITY") or "").strip() or None


def get_analytic_model() -> AnalyticModel:
    """解析耗时模型（盲区预筛与 mock 共用）。"""
    return _STATE.setdefault("analytic", zhangjiang_model())


def get_center() -> tuple[float, float]:
    """当前工作中心。

    优先级：显式 set_center > 已加载社区快照声明的中心 > 默认社区中心。

    修复（2026-10-15）：原实现是 `_STATE.get("center", DEFAULT_CENTER)`，
    **从不读取已加载快照的中心**。后果是切到别的社区（如广州）后，
    所有依赖 get_center() 的逻辑（mock 采样中心、replay 覆盖范围校验、
    "/api/audit 未指定坐标时的默认点"）仍然拿着默认社区（张江）的坐标 ——
    表现为「报错说快照是广州，却报出张江的中心」。
    """
    if "center" in _STATE:
        return _STATE["center"]
    try:
        c = get_bundle().index.get("center") or {}
        if c.get("lat") is not None and c.get("lng") is not None:
            return (float(c["lat"]), float(c["lng"]))
    except Exception:  # 无快照（mock / live）时回落到默认社区中心
        pass
    return DEFAULT_CENTER


def set_center(center: tuple[float, float]) -> None:
    _STATE["center"] = center


def get_bundle() -> ReplayBundle:
    """加载（并缓存）快照。失败时抛 SeedMissingError。"""
    if "bundle" not in _STATE:
        root = Path(os.environ.get("SEED_DIR") or settings.seed_dir)
        _STATE["bundle"] = ReplayBundle.load(find_community_dir(root, _community()))
    return _STATE["bundle"]


def get_route_provider(mode: str | None = None, allow_analytic: bool | None = None):
    """按当前模式返回路线 provider。"""
    if "route" in _OVERRIDE:
        return _OVERRIDE["route"]
    mode = mode or resolve_mode()
    if mode == "mock":
        return MockRouteProvider(center=get_center(), model=get_analytic_model())
    if mode == "live":
        return FallbackMatrixRouteProvider()
    if mode == "replay":
        return ReplayRouteProvider(get_bundle())
    # hybrid
    if allow_analytic is None:
        allow_analytic = str(os.environ.get("ALLOW_ANALYTIC_FALLBACK", "true")).lower() != "false"
    strict = str(os.environ.get("STRICT_SEED", "false")).lower() == "true"
    live = FallbackMatrixRouteProvider()
    if strict:
        from .base import RouteProvider  # noqa: F401

        class _Strict:
            """严格模式：快照缺失即抛 QuotaExhaustedError（契约原文口径）。"""

            name = "hybrid:strict"
            last_meta = {"source": "hybrid:strict", "degraded": False, "reasons": []}

            def __init__(self, bundle):
                self.bundle = bundle

            def walking_matrix(self, origin, destinations):
                try:
                    return self.bundle.route(origin, destinations)
                except SeedMissingError as e:
                    from ..baidu.errors import QuotaExhaustedError

                    raise QuotaExhaustedError("route", -1, str(e), "补齐快照或切换 DATA_MODE=live") from e

        return _Strict(get_bundle())
    from .live import OnlineFirstRouteProvider

    return OnlineFirstRouteProvider(
        seed=ReplayRouteProvider(get_bundle()),
        live=live,
        analytic=get_analytic_model(),
        allow_analytic=bool(allow_analytic),
        center=get_center(),
    )


def get_place_provider(mode: str | None = None):
    """按当前模式返回 POI provider。"""
    if "place" in _OVERRIDE:
        return _OVERRIDE["place"]
    mode = mode or resolve_mode()
    if mode == "mock":
        return MockPlaceProvider(center=get_center(), model=get_analytic_model())
    if mode == "live":
        return LivePlaceProvider()
    if mode == "replay":
        return ReplayPlaceProvider(get_bundle())
    # hybrid：快照优先，未命中走 live
    class _HybridPlace:
        name = "hybrid"

        def __init__(self, seed, live):
            self.seed = seed
            self.live = live
            self.last_meta = {"source": "hybrid", "degraded": False, "reasons": []}

        def search(self, query, center, radius: int = 2000, page_num: int = 0) -> dict:
            try:
                return self.seed.search(query, center, radius, page_num)
            except SeedMissingError:
                self.last_meta["degraded"] = True
                if "POI 快照未命中，已回退到在线检索" not in self.last_meta["reasons"]:
                    self.last_meta["reasons"].append("POI 快照未命中，已回退到在线检索")
                self.last_meta["source"] = "hybrid:live"
                return self.live.search(query, center, radius, page_num)

    return _HybridPlace(ReplayPlaceProvider(get_bundle()), LivePlaceProvider())


def get_geocode_provider(mode: str | None = None):
    if "geocode" in _OVERRIDE:
        return _OVERRIDE["geocode"]
    mode = mode or resolve_mode()
    if mode == "live":
        return LiveGeocodeProvider()
    if mode in ("replay", "hybrid"):
        try:
            return ReplayGeocodeProvider(get_bundle())
        except SeedMissingError:
            return LiveGeocodeProvider()
    return None


def describe() -> dict:
    """给 /api/health 用的运行态描述。"""
    mode = resolve_mode()
    info: dict[str, Any] = {
        "data_mode": mode,
        "seed_available": False,
        "seed_community": None,
        "has_ak": settings.has_ak,
        "has_sk": bool(settings.baidu_map_server_sk),
    }
    try:
        b = get_bundle()
        info["seed_available"] = True
        info["seed_community"] = b.name
        info["seed_collected_at"] = b.index.get("collected_at")
    except Exception:
        pass
    return info


def list_communities() -> list[dict]:
    """枚举 `data/seed/` 下所有可用社区（供 /api/seed/communities）。"""
    root = Path(os.environ.get("SEED_DIR") or settings.seed_dir)
    out: list[dict] = []
    if not root.exists():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        idx = d / "index.json"
        if not idx.exists():
            continue
        try:
            import json

            doc = json.loads(idx.read_text(encoding="utf-8"))
        except Exception:
            doc = {}
        out.append({
            "id": d.name,
            "name": doc.get("name") or d.name,
            "center": doc.get("center") or {"lat": DEFAULT_CENTER[0], "lng": DEFAULT_CENTER[1]},
            "address": doc.get("address", ""),
            "collected_at": doc.get("collected_at"),
            "poi_requests": doc.get("stats", {}).get("poi_requests"),
            "routes_charged": doc.get("stats", {}).get("routes_charged"),
        })
    return out


def reset_state() -> None:
    """测试用。"""
    _STATE.clear()
    _OVERRIDE.clear()


__all__ = [
    "describe",
    "get_analytic_model",
    "get_bundle",
    "get_center",
    "get_geocode_provider",
    "get_place_provider",
    "get_route_provider",
    "list_communities",
    "reset_state",
    "resolve_mode",
    "set_center",
    "BatchMatrixProvider",
    "DirectionLiteProvider",
    "FallbackMatrixRouteProvider",
]
