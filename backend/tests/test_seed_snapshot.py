"""seed 快照的完整性与可回放性（NFR-3）。**零网络。**"""
from __future__ import annotations

from tests._helpers import BACKEND, SEED_DIR  # noqa: F401

import hashlib
import json

from app.providers.replay import ReplayBundle, find_community_dir, route_key


def _index() -> dict:
    return json.loads((SEED_DIR / "zhangjiang-haike" / "index.json").read_text(encoding="utf-8"))


def test_seed_dir_structure():
    d = find_community_dir(SEED_DIR, "zhangjiang-haike")
    for name in ("index.json", "geocode.json", "poi.json", "routes/matrix.json"):
        assert (d / name).exists(), f"缺少快照文件 {name}"


def test_seed_index_has_required_metadata():
    idx = _index()
    for key in ("id", "name", "address", "center", "collected_at", "provider",
                "sample_config", "stats", "expected"):
        assert key in idx, f"index.json 缺少 {key}"
    assert idx["sample_config"]["rays"] == 36
    assert idx["sample_config"]["dr"] == 200.0
    assert idx["sample_config"]["densify_per_crossing"] == 3


def test_seed_expected_matches_snapshot_run():
    """黄金文件回归：index.json 的 expected 必须与当前算法跑出来的结果一致。"""
    idx = _index()
    exp = idx["expected"]
    assert exp["cleaning_funnel"] == {"raw": 32, "deduped": 31, "corrected": 30, "inside": 25}
    assert exp["blindspot_count"] >= 1
    assert 50 < exp["score_total"] <= 100
    assert exp["score_grade"] in ("A", "B", "C", "D")


def test_seed_file_hashes_recorded():
    idx = _index()
    assert idx["files"], "应记录快照文件哈希（防篡改 / 可审计）"
    for name, meta in idx["files"].items():
        p = SEED_DIR / "zhangjiang-haike" / name
        h = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        assert h == meta["sha256_16"], f"{name} 哈希与 index.json 不符"


def test_seed_poi_has_two_group_queries():
    poi = json.loads((SEED_DIR / "zhangjiang-haike" / "poi.json").read_text(encoding="utf-8"))
    assert len(poi) == 2, f"2 次请求覆盖 8 类，实际 {len(poi)} 组"
    for query, doc in poi.items():
        assert doc["status"] == 0
        assert isinstance(doc["results"], list)
        assert "$" in query, "组内必须是多关键字并集"


def test_seed_routes_cover_all_recorded_requests():
    b = ReplayBundle.load(find_community_dir(SEED_DIR, "zhangjiang-haike"))
    assert len(b.routes) >= 10, f"应录制等时圈+覆盖+盲区全部算路请求，实际 {len(b.routes)}"
    total = 0
    for key, rec in b.routes.items():
        assert len(rec["origin"]) == 2
        assert len(rec["destinations"]) == len(rec["durations"])
        assert len(rec["destinations"]) <= 50, "步行批量算路单次最多 50 条"
        assert key == route_key(tuple(rec["origin"]), [tuple(d) for d in rec["destinations"]])
        total += len(rec["durations"])
    assert total >= 400, f"总路线数应不少于 400，实际 {total}"


def test_seed_has_no_secrets():
    """快照绝不能包含 AK / SK。"""
    import re

    pat = re.compile(r"(ak=|sk=|BAIDU_MAP_SERVER|LLM_API_KEY|sk-[0-9a-f]{16,})")
    for f in (SEED_DIR / "zhangjiang-haike").rglob("*"):
        if f.is_file():
            text = f.read_text(encoding="utf-8", errors="ignore")
            assert not pat.search(text), f"{f.name} 疑似包含密钥"
