# -*- coding: utf-8 -*-
"""生成 backend/bench/audit_sample.json —— mock provider 下 GET /api/audit 的完整返回。

用 FastAPI TestClient 走真实路由（含 Pydantic 响应校验），而不是直接调 run_audit，
这样产物就是前端实际会拿到的那份 JSON。
"""
from __future__ import annotations

import json, os, sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

os.environ["DATA_MODE"] = "replay"
os.environ["SEED_DIR"] = str(ROOT / "data" / "seed")
os.environ["LLM_ENABLED"] = "false"   # 快照样例不打真实 LLM（省时、可复现）

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
r = client.get("/api/audit", params={"lat": 31.2045, "lng": 121.5990})
assert r.status_code == 200, r.text
body = r.json()
out = ROOT / "backend" / "bench" / "audit_sample.json"
out.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")

meta = body["meta"]
print("written", out, out.stat().st_size, "bytes")
print("data_mode      :", meta["data_mode"])
print("degraded       :", meta["degraded"], "|", len(meta["degrade_reasons"]), "reasons")
print("quota          :", meta["quota"])
print("isochrones     :", {k: round(v["area_m2"]) for k, v in body["isochrones"].items()})
print("funnel         :", body["facilities"]["cleaning_funnel"])
print("blindspots     :", len(body["blindspots"]["polygons"]), round(body["blindspots"]["total_area_m2"]))
print("score          :", body["score"]["total"], body["score"]["grade"])
print("siting         :", len(body["siting"]))
print("ai degraded    :", body["ai_report"]["degraded"])
