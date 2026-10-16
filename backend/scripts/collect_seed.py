"""采集样例社区快照到 `data/seed/<community>/`，供 `DATA_MODE=replay` 零配额回放。

用法：

    # 零配额：用 mock provider 生成一份「合成社区」快照（CI / 评审方开箱可跑）
    .venv/Scripts/python.exe scripts/collect_seed.py --provider mock

    # 真实采集（会消耗配额：POI 2 次 + 算路按采样点数计）
    .venv/Scripts/python.exe scripts/collect_seed.py --provider live --lat 31.2045 --lng 121.5990

产出结构：

    data/seed/<community>/
      ├─ index.json          元信息（社区名 / 中心 / 采集时间 / 统计 / SHA256 清单）
      ├─ geocode.json        地址 -> BD09
      ├─ poi.json            {query: 百度原始响应}
      └─ routes/matrix.json  {请求哈希: {origin, destinations, durations}}

**采集口径**：先跑一遍完整审计（等时圈采样 + POI 检索 + 覆盖算路 + 盲区边界带校正），
把所有经过 provider 的请求原样录下来。这样 replay 时不仅等时圈能复现，
覆盖统计与盲区校正的算路也全部命中快照 —— 真正零配额、且与 live 结果一致。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

# ---- Windows 控制台编码修复：不做这一步，GBK 控制台遇到 emoji / 中文会直接崩 ----
if sys.platform == "win32":
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001
        pass
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from app.config import settings  # noqa: E402
from app.providers.base import RecordingRouteProvider  # noqa: E402
from app.providers.mock import DEFAULT_CENTER  # noqa: E402
from app.providers.replay import route_key  # noqa: E402

CST = timezone(timedelta(hours=8))


class RecordingPlaceProvider:
    """包住任意 PlaceProvider，记录每一次检索的原始响应。"""

    def __init__(self, inner):
        self.inner = inner
        self.name = getattr(inner, "name", "wrapped")
        self.responses: dict[str, dict] = {}

    @property
    def last_meta(self):
        return getattr(self.inner, "last_meta", {"source": self.name, "degraded": False, "reasons": []})

    def search(self, query: str, center, radius: int = 2000, page_num: int = 0) -> dict:
        doc = self.inner.search(query, center, radius=radius, page_num=page_num)
        if page_num == 0:
            self.responses[query] = doc
        return doc


def _build_providers(provider: str, center):
    if provider == "live":
        from app.providers.live import FallbackMatrixRouteProvider, LivePlaceProvider

        if not settings.has_ak:
            raise SystemExit("未配置 BAIDU_MAP_SERVER_AK，无法 live 采集；先用 --provider mock")
        return FallbackMatrixRouteProvider(), LivePlaceProvider(), "live"
    from app.providers.mock import MockPlaceProvider, MockRouteProvider

    return MockRouteProvider(center=center), MockPlaceProvider(center=center), "mock"


def _sha256(path: Path) -> str:
    """快照文件哈希（**换行符无关**）。

    为什么必须归一化：Windows 上 write_text 会把 \n 翻成 \r\n，而 CI（Linux）
    检出后是 \n —— 直接对 read_bytes() 取哈希，同一份内容在两端算出的值不同，
    test_seed_snapshot.py::test_seed_file_hashes_recorded 会在 CI 上假失败
    （实测 2026-10-16：本地 132 全绿，CI 报 poi.json 哈希不符）。
    统一按 LF 归一化后再哈希，两端一致。
    """
    h = hashlib.sha256()
    h.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser(description="采集社区快照到 data/seed/")
    ap.add_argument("--community", default="zhangjiang-haike", help="社区 id（目录名）")
    ap.add_argument("--name", default="上海浦东 · 张江海科路社区", help="社区显示名")
    ap.add_argument("--address", default="上海市浦东新区海科路1650号")
    ap.add_argument("--lat", type=float, default=DEFAULT_CENTER[0])
    ap.add_argument("--lng", type=float, default=DEFAULT_CENTER[1])
    ap.add_argument("--provider", choices=("mock", "live"), default="mock",
                    help="mock = 零配额合成快照；live = 真实调用百度 API（消耗配额）")
    ap.add_argument("--radius", type=int, default=None, help="POI 检索半径（米），默认按等时圈外接圆 ×1.1")
    ap.add_argument("--seed-dir", default=None)
    ap.add_argument("--no-verify", action="store_true", help="跳过采集后的 replay 复跑校验")
    args = ap.parse_args()

    center = (float(args.lat), float(args.lng))
    seed_root = Path(args.seed_dir) if args.seed_dir else Path(settings.seed_dir)
    out_dir = seed_root / args.community
    (out_dir / "routes").mkdir(parents=True, exist_ok=True)

    print(f"[1/4] 采集社区「{args.name}」 center=({center[0]:.6f}, {center[1]:.6f}) provider={args.provider}")
    route_raw, place_raw, mode = _build_providers(args.provider, center)
    route_provider = RecordingRouteProvider(route_raw)
    place_provider = RecordingPlaceProvider(place_raw)

    # ---- 让 registry 用录制 provider 跑一遍完整审计 ----
    from app.providers import registry

    os.environ["DATA_MODE"] = "mock" if mode == "mock" else "live"
    registry.reset_state()
    registry.set_center(center)
    registry.set_override(route=route_provider, place=place_provider)

    print("[2/4] 跑一遍完整审计并录制全部 provider 调用（等时圈采样 + POI + 覆盖算路 + 盲区校正）")
    t0 = time.perf_counter()
    from app.services.audit import AuditOptions, run_audit

    outcome = run_audit(AuditOptions(lat=center[0], lng=center[1],
                                     address=args.address if mode == "live" else None))
    rep = outcome.report
    elapsed = time.perf_counter() - t0
    iso_stats = {k: round(v["area_m2"] / 1e6, 4) for k, v in rep["isochrones"].items()}
    print(f"      等时圈面积 km²: {iso_stats}")
    print(f"      清洗漏斗: {rep['facilities']['cleaning_funnel']}")
    print(f"      盲区: {len(rep['blindspots']['polygons'])} 个 / "
          f"{rep['blindspots']['total_area_m2'] / 1e6:.3f} km²；评分 {rep['score']['total']}")
    print(f"      录制到 {len(route_provider.requests)} 次算路请求 / "
          f"{route_provider.destination_count} 条路线 / {len(place_provider.responses)} 组 POI 响应")

    # ---- 写快照 ----
    print("[3/4] 写入快照")
    routes: dict[str, dict] = {}
    for req, durations in zip(route_provider.requests, route_provider.responses):
        origin = tuple(req["origin"])
        dests = [tuple(d) for d in req["destinations"]]
        routes[route_key(origin, dests)] = {
            "origin": list(origin),
            "destinations": [list(d) for d in dests],
            # 直接用录制时的原始耗时，**不再二次调用 API**（live 模式下这会翻倍烧配额）
            # 三位小数（毫秒级）：两位小数在 125 m 格网的阈值判定上会翻转个别格网的
            # 「盲 / 不盲」结论，造成 replay 与采集结果有 1~2 格的假差异。
            "durations": [None if v is None else round(float(v), 3) for v in durations],
        }
    (out_dir / "routes" / "matrix.json").write_text(
        json.dumps(routes, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out_dir / "poi.json").write_text(
        json.dumps(place_provider.responses, ensure_ascii=False, indent=1), encoding="utf-8")

    geocode = {args.address: {"lat": center[0], "lng": center[1]}}
    if mode == "live":
        try:
            from app.baidu.client import get_client

            hit = get_client().geocode(args.address)
            if hit:
                geocode[args.address] = {"lat": hit[0], "lng": hit[1]}
                print(f"      地理编码：{args.address} -> {hit[0]:.6f}, {hit[1]:.6f}")
        except Exception as e:  # noqa: BLE001
            print(f"      地理编码失败（{type(e).__name__}: {e}），沿用命令行坐标")
    (out_dir / "geocode.json").write_text(json.dumps(geocode, ensure_ascii=False, indent=1), encoding="utf-8")

    cfg = registry.get_analytic_model()
    index = {
        "id": args.community,
        "community": args.community,
        "name": args.name,
        "address": args.address,
        "center": {"lat": center[0], "lng": center[1]},
        "collected_at": datetime.now(CST).isoformat(timespec="seconds"),
        "provider": mode,
        "sample_config": {"rays": 36, "dr": 200.0, "densify_per_crossing": 3, "grid_res": 25.0},
        "barriers": cfg.as_dict()["barriers"],
        "stats": {
            "route_requests": len(route_provider.requests),
            "routes_charged": route_provider.destination_count,
            "poi_requests": len(place_provider.responses),
            "route_snapshots": len(routes),
            "elapsed_s": round(elapsed, 2),
        },
        "expected": {
            "isochrone_area_km2": iso_stats,
            "cleaning_funnel": rep["facilities"]["cleaning_funnel"],
            "blindspot_count": len(rep["blindspots"]["polygons"]),
            "blindspot_area_km2": round(rep["blindspots"]["total_area_m2"] / 1e6, 4),
            "score_total": rep["score"]["total"],
            "score_grade": rep["score"]["grade"],
        },
        "files": {},
        "notes": [
            "等时圈一次采样服务 5/10/15 分钟三档（省 2.3 倍配额）",
            "POI 用 2 组多关键字并集检索覆盖 8 类（按请求计费，D1 闸门 G1 实测确认）",
            "快照覆盖等时圈采样 + 覆盖统计算路 + 盲区边界带校正，replay 可完全复现",
        ],
    }
    for f in ("poi.json", "geocode.json", "routes/matrix.json"):
        p = out_dir / f
        if p.exists():
            index["files"][f] = {"bytes": p.stat().st_size, "sha256_16": _sha256(p)}
    (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    (seed_root / "index.json").write_text(
        json.dumps({"default": args.community, "updated_at": index["collected_at"]}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"      ✅ {out_dir}")
    print(f"         routes/matrix.json {len(routes)} 条 / poi.json {len(place_provider.responses)} 组")

    if args.no_verify:
        return 0

    # ---- 复跑校验：用 replay provider 走一遍，确认零配额可复现 ----
    print("[4/4] 用 replay provider 复跑校验（零配额）")
    os.environ["DATA_MODE"] = "replay"
    os.environ["SEED_DIR"] = str(seed_root)
    os.environ["SEED_COMMUNITY"] = args.community
    registry.reset_state()
    registry.set_center(center)
    try:
        again = run_audit(AuditOptions(lat=center[0], lng=center[1]))
    except Exception as e:  # noqa: BLE001
        print(f"      ❌ replay 复跑失败：{type(e).__name__}: {e}")
        return 1
    r2 = again.report
    ok = True
    for k, iso in r2["isochrones"].items():
        exp = iso_stats.get(k)
        got = round(iso["area_m2"] / 1e6, 4)
        if (iso.get("area_m2") or 0) <= 0:
            ok = False
            print(f"      ❌ {k} 分钟等时圈为空")
        elif exp and abs(got - exp) / max(exp, 1e-9) > 0.01:
            ok = False
            print(f"      ❌ {k} 分钟等时圈面积不一致：live={exp} replay={got}")
    f1, f2 = rep["facilities"]["cleaning_funnel"], r2["facilities"]["cleaning_funnel"]
    if f1 != f2:
        ok = False
        print(f"      ❌ 清洗漏斗不一致：live={f1} replay={f2}")
    b1 = round(rep["blindspots"]["total_area_m2"] / 1e6, 3)
    b2 = round(r2["blindspots"]["total_area_m2"] / 1e6, 3)
    print(f"      等时圈面积： " + "，".join(f"{k}min={v['area_m2'] / 1e6:.3f} km²"
                                            for k, v in r2["isochrones"].items()))
    print(f"      清洗漏斗： {f2}")
    print(f"      盲区：     {len(r2['blindspots']['polygons'])} 个，合计 {b2:.3f} km²（采集时 {b1:.3f}）")
    print(f"      评分：     {r2['score']['total']}（{r2['score']['grade']}）"
          f"（采集时 {rep['score']['total']}）")
    print(f"      降级原因： {'；'.join(r2['meta']['degrade_reasons']) or '无'}")
    print(f"      记录 {len(again.notes)} 条；耗时 {again.elapsed_s:.2f}s")
    print("      " + ("✅ replay 复跑通过（与采集结果一致，零配额）" if ok else "❌ replay 复跑与采集结果不一致"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
