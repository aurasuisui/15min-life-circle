"""文件缓存：参数哈希键 + TTL。**命中不消耗配额。**

键 = sha256(service ‖ 归一化参数 ‖ 数据版本)。坐标做 ≈1m 量化（保留 5 位小数）
后再入键，显著提升命中率（同一社区重复体检几乎 100% 命中）。

TTL 依据 SOLUTION.md §5.1：地理编码 90 天 / POI 7 天 / 算路 30 天 / 坐标转换 30 天。

存储：默认 SQLite（`data/cache/cache.db`），`backend=json` 时退化为
「一个条目一个 json 文件 + 索引」，便于人工翻看快照。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

TTL_GEOCODE_S = 90 * 24 * 3600
TTL_PLACE_S = 7 * 24 * 3600
TTL_ROUTE_S = 30 * 24 * 3600
TTL_GEOCONV_S = 30 * 24 * 3600

DEFAULT_TTL = {"geocode": TTL_GEOCODE_S, "place": TTL_PLACE_S, "route": TTL_ROUTE_S, "geoconv": TTL_GEOCONV_S}


def quantize(value: Any, ndigits: int = 5) -> Any:
    """把浮点坐标量化到 ≈1 m，提升缓存命中率。递归处理 list/tuple/dict。"""
    if isinstance(value, float):
        return round(value, ndigits)
    if isinstance(value, (list, tuple)):
        return [quantize(v, ndigits) for v in value]
    if isinstance(value, dict):
        return {k: quantize(v, ndigits) for k, v in sorted(value.items())}
    return value


def cache_key(service: str, params: dict, version: str = "v1") -> str:
    payload = json.dumps(quantize(params), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{service}|{payload}|{version}".encode("utf-8")).hexdigest()


class FileCache:
    """带 TTL 的持久化缓存。"""

    def __init__(self, root: str | Path, backend: str = "sqlite", enabled: bool = True):
        self.root = Path(root)
        self.backend = backend
        self.enabled = enabled
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()
        self._mem: dict[str, tuple[float, Any]] = {}
        self._db_path = self.root / "cache.db"
        self._json_dir = self.root / "entries"
        if not self.enabled:
            return
        if backend == "sqlite":
            self.root.mkdir(parents=True, exist_ok=True)
            with contextlib.closing(self._connect()) as c:
                c.execute(
                    "CREATE TABLE IF NOT EXISTS cache_entries ("
                    " key TEXT PRIMARY KEY, service TEXT, response_json TEXT,"
                    " created_at REAL, ttl_s REAL, hits INTEGER DEFAULT 0)"
                )
                c.commit()
        else:
            self._json_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 内部 ----------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=15.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextlib.contextmanager
    def _conn(self):
        """短连接上下文：**用完必须关**。

        ⚠️ Windows 上 sqlite3 的连接对象既不是上下文管理器（`with conn` 只管事务、
        不关句柄），也不会随 GC 立即释放文件锁 —— 不显式 close() 会导致
        `PermissionError: [WinError 32]`（临时目录 / 工作目录删不掉）。
        """
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def close(self) -> None:
        """清空内存缓存（持久层是短连接，无长驻句柄）。"""
        self.enabled = False
        with self._lock:
            self._mem.clear()

    # ---------------- 公开 API ----------------
    def get(self, key: str) -> Any | None:
        if not self.enabled:
            return None
        with self._lock:
            hit = self._mem.get(key)
            if hit:
                expires, value = hit
                if expires > time.time():
                    self.hits += 1
                    return value
                self._mem.pop(key, None)

        value = self._get_persisted(key)
        if value is None:
            self.misses += 1
            return None
        self.hits += 1
        return value

    def set(self, key: str, service: str, value: Any, ttl_s: float | None = None) -> None:
        if not self.enabled:
            return
        ttl = float(ttl_s if ttl_s is not None else DEFAULT_TTL.get(service, TTL_PLACE_S))
        expires = time.time() + ttl
        with self._lock:
            self._mem[key] = (expires, value)
        if self.backend == "sqlite":
            try:
                with self._conn() as c:
                    c.execute(
                        "INSERT OR REPLACE INTO cache_entries"
                        " (key, service, response_json, created_at, ttl_s, hits) VALUES (?,?,?,?,?,0)",
                        (key, service, json.dumps(value, ensure_ascii=False), time.time(), ttl),
                    )
            except Exception:
                pass  # 缓存写失败绝不影响主流程
        else:
            try:
                (self._json_dir / f"{key}.json").write_text(
                    json.dumps({"service": service, "created_at": time.time(), "ttl_s": ttl, "value": value},
                               ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

    # ---------------- 持久化读取 ----------------
    def _get_persisted(self, key: str) -> Any | None:
        try:
            if self.backend == "sqlite":
                with self._conn() as c:
                    row = c.execute(
                        "SELECT response_json, created_at, ttl_s FROM cache_entries WHERE key=?", (key,)
                    ).fetchone()
                    if not row:
                        return None
                    body, created, ttl = row
                    if time.time() > created + ttl:
                        c.execute("DELETE FROM cache_entries WHERE key=?", (key,))
                        return None
                    c.execute("UPDATE cache_entries SET hits = hits + 1 WHERE key=?", (key,))
                value = json.loads(body)
            else:
                f = self._json_dir / f"{key}.json"
                if not f.exists():
                    return None
                doc = json.loads(f.read_text(encoding="utf-8"))
                if time.time() > doc["created_at"] + doc["ttl_s"]:
                    f.unlink(missing_ok=True)
                    return None
                value = doc["value"]
        except Exception:
            return None
        with self._lock:
            ttl = DEFAULT_TTL.get("place", TTL_PLACE_S)
            self._mem[key] = (time.time() + ttl, value)
        return value

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
            "backend": self.backend,
        }
