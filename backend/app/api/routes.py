"""API 路由（CONTRACT §5）。

| 方法 | 路径              | 说明                                   |
| GET  | /api/health       | {"status","data_mode","llm"}           |
| GET  | /api/geocode      | 地址 -> {lat,lng,address}              |
| GET  | /api/audit        | **完整体检报告 JSON（§4）**            |
| GET  | /api/isochrone    | 单个等时圈对象                          |
| GET  | /api/seed/communities | 可用离线社区列表                    |

⚠️ 所有同步重活都通过 `asyncio.to_thread` 丢到线程池，
   避免阻塞事件循环（等时圈在 live 模式下要几十秒）。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Query

from ..config import ROOT, settings
from ..constants import DEFAULT_MINUTES, FACILITY_CATEGORIES
from ..providers import get_geocode_provider, list_communities, resolve_mode
from ..providers.base import SeedMissingError
from ..schemas.api import CommunitiesResponse, GeocodeResponse, HealthResponse
from ..services.audit import AuditOptions, run_audit

router = APIRouter(prefix="/api", tags=["audit"])


def _llm_enabled() -> bool:
    """LLM 可用性。

    ⚠️ 必须走 S4 提供的 `app.ai.ai_health()`，**不要**读 `settings.llm_enabled`
    —— `Settings` 里没有该字段（LLM_* 由 app/ai 自己的 LLMConfig.from_env() 加载 .env），
    读它会永远得到 False，前端「AI 可用」徽标会显示错误状态。
    `ai_health()` 不发网络请求，零成本。
    """
    try:
        from app.ai import ai_health  # type: ignore

        return bool(ai_health().get("llm", False))
    except Exception:
        return False


def _strip_debug(report: dict, debug: bool) -> dict:
    if debug:
        return report
    return {k: v for k, v in report.items() if not k.startswith("_")}


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    from ..providers import describe

    info = describe()
    return HealthResponse(
        status="ok",
        data_mode=info["data_mode"],
        llm=_llm_enabled(),
        seed_available=bool(info.get("seed_available")),
        seed_community=info.get("seed_community"),
        seed_collected_at=info.get("seed_collected_at"),
        has_ak=bool(info.get("has_ak")),
        has_sk=bool(info.get("has_sk")),
    )


@router.get("/geocode", response_model=GeocodeResponse)
async def geocode(address: str = Query(..., min_length=1, description="地址，例如 上海市浦东新区海科路1650号"),
                  city: str = Query("上海市")) -> GeocodeResponse:
    mode = resolve_mode()
    if mode == "mock":
        # 零网络：确定性哈希映射到样例社区内的一个点
        import math

        from ..providers import get_center

        base = get_center()
        h = sum(ord(c) for c in address)
        dx = ((h % 9) - 4) * 40.0
        dy = (((h // 9) % 9) - 4) * 40.0
        m_lat = 111132.0
        m_lng = m_lat * math.cos(math.radians(base[0]))
        return GeocodeResponse(lat=base[0] + dy / m_lat, lng=base[1] + dx / m_lng, address=address)

    gp = get_geocode_provider(mode)
    if gp is None:
        raise ValueError("当前模式不支持地理编码")
    hit = await asyncio.to_thread(gp.geocode, address)
    if not hit:
        raise SeedMissingError(f"未解析出坐标：{address}")
    return GeocodeResponse(lat=float(hit[0]), lng=float(hit[1]), address=address)


@router.get("/audit")
async def audit(
    lat: float | None = Query(None, ge=-90, le=90),
    lng: float | None = Query(None, ge=-180, le=180),
    address: str | None = Query(None),
    minutes: str = Query("5,10,15", description="逗号分隔的时间档位（分钟）"),
    mode: str | None = Query(None, description="覆盖 DATA_MODE：mock|replay|live|hybrid"),
    debug: bool = Query(False, description="附带 _debug 段（清洗日志 / 耗时 / 配额明细）"),
) -> dict:
    if lat is None or lng is None:
        if not address:
            raise ValueError("必须提供 lat+lng 或 address")
    try:
        mins = tuple(int(x) for x in str(minutes).replace("，", ",").split(",") if x.strip())
    except ValueError as e:
        raise ValueError(f"minutes 参数非法：{minutes}") from e
    if not mins or any(m <= 0 or m > 60 for m in mins):
        mins = DEFAULT_MINUTES

    opts = AuditOptions(lat=lat, lng=lng, address=address, minutes=mins, mode=mode)
    outcome = await asyncio.to_thread(run_audit, opts)
    return _strip_debug(outcome.report, debug)


@router.get("/isochrone")
async def isochrone(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    minutes: int = Query(15, ge=1, le=60),
    mode: str | None = Query(None),
    debug: bool = Query(False),
) -> dict:
    opts = AuditOptions(lat=lat, lng=lng, minutes=(minutes,), mode=mode)
    outcome = await asyncio.to_thread(run_audit, opts)
    payload = outcome.report.get("isochrones", {}).get(str(minutes))
    if payload is None:
        raise ValueError(f"未能生成 {minutes} 分钟等时圈")
    result = {"meta": {"center": outcome.report["meta"]["center"],
                       "data_mode": outcome.report["meta"]["data_mode"],
                       "degraded": outcome.report["meta"]["degraded"],
                       "degrade_reasons": outcome.report["meta"]["degrade_reasons"]},
              "isochrone": payload}
    if debug:
        result["_debug"] = outcome.report.get("_debug", {})
    return result


@router.get("/seed/communities", response_model=CommunitiesResponse)
async def seed_communities() -> CommunitiesResponse:
    return CommunitiesResponse(communities=list_communities())


@router.get("/categories")
async def categories() -> dict:
    """8 类设施与检索词（冻结契约的可机读副本，前端与文档可直接引用）。"""
    return {"categories": [
        {"key": k, "name": v["name"], "query": v["query"], "group": v["group"]}
        for k, v in FACILITY_CATEGORIES.items()
    ]}


@router.get("/bench")
async def bench(limit: int = Query(3, ge=1, le=20)) -> dict:
    """读取 backend/bench 下的基准产物（只读，供前端「工程优化」展示页使用）。"""
    out: dict = {"files": []}
    bdir = Path(ROOT) / "backend" / "bench"
    if bdir.exists():
        for f in sorted(bdir.glob("*.json"))[:limit]:
            try:
                out["files"].append({"name": f.name, "content": json.loads(f.read_text(encoding="utf-8"))})
            except Exception:
                continue
    return out
