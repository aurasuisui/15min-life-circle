"""数据来源抽象层：live | replay | mock | hybrid 四态。"""
from __future__ import annotations

from .base import (
    PlaceProvider,
    ProviderError,
    RecordingRouteProvider,
    RouteProvider,
    SeedMissingError,
    meta_of,
)
from .barriers import AnalyticModel, BarrierLine, zhangjiang_model
from .live import (
    BatchMatrixProvider,
    DirectionLiteProvider,
    FallbackMatrixRouteProvider,
    LiveGeocodeProvider,
    LivePlaceProvider,
    OnlineFirstRouteProvider,
)
from .mock import DEFAULT_CENTER, MockPlaceProvider, MockRouteProvider
from .registry import (
    clear_override,
    describe,
    get_analytic_model,
    get_bundle,
    get_center,
    get_geocode_provider,
    get_place_provider,
    get_route_provider,
    list_communities,
    reset_state,
    resolve_mode,
    set_center,
    set_override,
)
from .replay import (
    ReplayBundle,
    ReplayGeocodeProvider,
    ReplayPlaceProvider,
    ReplayRouteProvider,
    find_community_dir,
    route_key,
)

__all__ = [
    "AnalyticModel",
    "BarrierLine",
    "BatchMatrixProvider",
    "DEFAULT_CENTER",
    "DirectionLiteProvider",
    "FallbackMatrixRouteProvider",
    "LiveGeocodeProvider",
    "LivePlaceProvider",
    "MockPlaceProvider",
    "MockRouteProvider",
    "OnlineFirstRouteProvider",
    "PlaceProvider",
    "ProviderError",
    "RecordingRouteProvider",
    "ReplayBundle",
    "ReplayGeocodeProvider",
    "ReplayPlaceProvider",
    "ReplayRouteProvider",
    "RouteProvider",
    "SeedMissingError",
    "describe",
    "find_community_dir",
    "get_analytic_model",
    "get_bundle",
    "get_center",
    "get_geocode_provider",
    "get_place_provider",
    "get_route_provider",
    "list_communities",
    "meta_of",
    "reset_state",
    "resolve_mode",
    "route_key",
    "clear_override",
    "set_center",
    "set_override",
    "zhangjiang_model",
]
