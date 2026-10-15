#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重标定快照里的 expected 块（零配额）。

== 为什么需要这个工具 ==

`data/seed/<community>/index.json` 里的 `expected` 记录的是「采集当时算法跑出来的结果」，
`tests/test_audit_pipeline.py::test_replay_audit_matches_recorded_expectations`
用它验证「replay 回放能复现采集结果」，是很重要的确定性守卫。

但当**算法本身被改进**时（例如 2026-10-15 给耗时场加了高斯平滑，用于抑制
directionlite 逐点路网吸附造成的锯齿噪声），采集当时的数值就不再是正确基准了 ——
不是说回放坏了，而是「如果现在去采集，得到的本来就会是新数值」。

此时应当**重新标定** expected，而不是放宽测试容差：
放宽容差会让守卫逐渐失效；重标定则保留「同一算法下回放必须逐位一致」的强度。

== 用法（零 API 配额，纯 replay）==

    python backend/scripts/refresh_expected.py zhangjiang-haike
    python backend/scripts/refresh_expected.py --all

刷新后请跑 `pytest tests` 确认全绿，并在提交信息里说明为什么重标定。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.platform == "win32":
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:  # noqa: BLE001
        pass
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.services.audit import AuditOptions, run_audit  # noqa: E402

CST = timezone(timedelta(hours=8))


def refresh(community: str) -> int:
    seed_root = Path(settings.seed_dir)
    idx_path = seed_root / community / "index.json"
    if not idx_path.exists():
        print("  x 找不到快照: %s" % idx_path)
        return 1

    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    center = idx["center"]

    os.environ["DATA_MODE"] = "replay"
    os.environ["SEED_COMMUNITY"] = community
    import app.providers.registry as registry

    registry.reset_state()
    registry.set_center((float(center["lat"]), float(center["lng"])))

    out = run_audit(AuditOptions(lat=float(center["lat"]), lng=float(center["lng"])))
    rep = out.report

    old = idx.get("expected", {})
    new = {
        "isochrone_area_km2": {
            k: round(rep["isochrones"][k]["area_m2"] / 1e6, 4) for k in ("5", "10", "15")
        },
        "cleaning_funnel": rep["facilities"]["cleaning_funnel"],
        "blindspot_count": len(rep["blindspots"]["polygons"]),
        "blindspot_area_km2": round(rep["blindspots"]["total_area_m2"] / 1e6, 4),
        "score_total": rep["score"]["total"],
        "score_grade": rep["score"]["grade"],
        "rebased_at": datetime.now(CST).isoformat(timespec="seconds"),
        "rebased_note": "算法变更后重标定（见 scripts/refresh_expected.py 说明）",
    }
    idx["expected"] = new
    idx_path.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")

    print("  OK %s" % community)
    print("      等时圈 km2: 旧 %s -> 新 %s" % (old.get("isochrone_area_km2"), new["isochrone_area_km2"]))
    print("      评分:       旧 %s -> 新 %s" % (old.get("score_total"), new["score_total"]))
    print("      漏斗:       %s   盲区 %d 个" % (new["cleaning_funnel"], new["blindspot_count"]))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="重标定快照 expected 块（零配额）")
    ap.add_argument("community", nargs="?", help="社区 id")
    ap.add_argument("--all", action="store_true", help="刷新所有社区")
    args = ap.parse_args()

    seed_root = Path(settings.seed_dir)
    if args.all:
        targets = [d.name for d in seed_root.iterdir() if (d / "index.json").exists()]
    elif args.community:
        targets = [args.community]
    else:
        ap.error("请给出社区 id，或用 --all")

    rc = 0
    for c in targets:
        rc |= refresh(c)
    print("")
    print("完成。请跑 pytest tests 确认全绿。")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
