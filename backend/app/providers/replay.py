"""Replay Provider —— 从 `data/seed/` 读快照，**零配额**。

快照目录结构（`scripts/collect_seed.py` 产出）：

    data/seed/<community>/
      ├─ index.json                   # 元信息 + 地址 -> 坐标 + POI 按 query 索引
      ├─ geocode.json                 # 地址 -> BD09
      ├─ poi.json                     # {query: {radius: 百度原始响应}}
      └─ routes/matrix/<sha1>.json    # {"origin": [...], "destinations": [...], "durations": [...]}

键一律用「参数哈希」而不是顺序号 —— 只要参数一致，回放与采集就能对上，
不同社区之间也不会互相污染。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .base import SeedMissingError


def _hash(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _q5(v: float) -> float:
    return round(float(v), 5)


def route_key(origin: tuple[float, float], destinations: list[tuple[float, float]]) -> str:
    return _hash({
        "o": [_q5(origin[0]), _q5(origin[1])],
        "d": [[_q5(a), _q5(b)] for a, b in destinations],
    })


def find_community_dir(seed_dir: str | Path, community: str | None = None) -> Path:
    """定位某个社区的快照目录；community=None 时取默认社区。"""
    root = Path(seed_dir)
    if not root.exists():
        raise SeedMissingError(f"seed 目录不存在：{root}（请先跑 scripts/collect_seed.py）")
    if community:
        d = root / community
        if not (d / "index.json").exists():
            raise SeedMissingError(f"社区快照不存在：{community}")
        return d
    idx = root / "index.json"
    if idx.exists():
        try:
            default = json.loads(idx.read_text(encoding="utf-8")).get("default")
            if default and (root / default / "index.json").exists():
                return root / default
        except Exception:
            pass
    cands = sorted(p for p in root.iterdir() if p.is_dir() and (p / "index.json").exists())
    if not cands:
        raise SeedMissingError(f"{root} 下没有任何社区快照")
    return cands[0]


@dataclass
class ReplayBundle:
    """一个社区的快照集合。"""

    base_dir: Path
    index: dict = field(default_factory=dict)
    geocode: dict = field(default_factory=dict)
    poi: dict = field(default_factory=dict)
    routes: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return str(self.index.get("community") or self.base_dir.name)

    @property
    def center(self) -> tuple[float, float]:
        c = self.index.get("center") or {}
        return float(c.get("lat", 31.2045)), float(c.get("lng", 121.5990))

    @classmethod
    def load(cls, base_dir: Path) -> "ReplayBundle":
        def _rd(p: Path) -> dict:
            if not p.exists():
                return {}
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {}

        routes: dict = {}
        rdir = base_dir / "routes"
        if rdir.is_dir():
            # 单文件 routes/matrix.json（新格式）
            routes.update(_rd(rdir / "matrix.json"))
            # 兼容「一个请求一个文件」的旧格式
            sub = rdir / "matrix"
            if sub.is_dir():
                for f in sorted(sub.glob("*.json")):
                    routes[f.stem] = _rd(f)
        return cls(
            base_dir=base_dir,
            index=_rd(base_dir / "index.json"),
            geocode=_rd(base_dir / "geocode.json"),
            poi=_rd(base_dir / "poi.json"),
            routes=routes,
        )

    def route(self, origin, destinations) -> list[float | None]:
        key = route_key(origin, destinations)
        hit = self.routes.get(key)
        if hit is None:
            raise SeedMissingError(
                f"快照缺失：routes/matrix.json 中没有 {key}"
                f"（origin={origin} destinations={len(destinations)} 条）"
            )
        durations = hit["durations"]
        if len(durations) < len(destinations):
            durations = list(durations) + [None] * (len(destinations) - len(durations))
        return durations


@dataclass
class ReplayRouteProvider:
    bundle: ReplayBundle
    name: str = "replay"

    def __post_init__(self) -> None:
        self.last_meta = {
            "source": "replay",
            "degraded": False,
            "reasons": [
                f"等时圈与可达性来自离线快照（社区：{self.bundle.name}，"
                f"采集于 {self.bundle.index.get('collected_at', '未知')}），本次零配额"
            ],
        }

    def walking_matrix(self, origin, destinations):
        return self.bundle.route(origin, destinations)


@dataclass
class ReplayPlaceProvider:
    bundle: ReplayBundle
    name: str = "replay"

    def __post_init__(self) -> None:
        self.last_meta = {
            "source": "replay",
            "degraded": False,
            "reasons": [f"POI 检索结果来自离线快照（社区：{self.bundle.name}），本次零配额"],
        }

    def search(self, query: str, center, radius: int = 2000, page_num: int = 0) -> dict:
        doc = self.bundle.poi.get(query)
        if doc is None:
            # 多关键字并集：按 keyword 拆分后做并集，命中不了就报缺失
            keys = [k for k in query.split("$") if k]
            merged: dict[str, dict] = {}
            for k in keys:
                part = self.bundle.poi.get(k)
                if part is None:
                    raise SeedMissingError(f"快照缺失：poi[{query}]（seed 目录 {self.bundle.base_dir}）")
                for item in part.get("results", []):
                    merged[str(item.get("uid") or item.get("name"))] = item
            return {"status": 0, "message": "replay(merged)", "results": list(merged.values()),
                    "total": len(merged)}
        return doc


@dataclass
class ReplayGeocodeProvider:
    bundle: ReplayBundle
    name: str = "replay"

    def __post_init__(self) -> None:
        self.last_meta = {"source": "replay", "degraded": False,
                          "reasons": ["地址解析来自离线快照，本次零配额"]}

    def geocode(self, address: str) -> tuple[float, float] | None:
        key = address.strip()
        hit = self.bundle.geocode.get(key)
        if hit:
            return float(hit["lat"]), float(hit["lng"])
        for k, v in self.bundle.geocode.items():
            if k and (k in key or key in k):
                return float(v["lat"]), float(v["lng"])
        raise SeedMissingError(f"快照缺失：geocode[{address}]")
