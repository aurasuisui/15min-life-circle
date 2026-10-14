"""QuotaLedger —— 每日配额账本（SQLite 落盘，支持 预留/提交/释放/熔断）。

设计要点（SOLUTION.md §5.1 第四道防线）：
  * 调用【前】reserve(n)：used + reserved + n <= limit 才放行；
  * 调用【后】commit(n) 成功计费 / release(n) 失败归还；
  * 进程重启不丢账（SQLite 持久化）；
  * 80% 黄灯、95% 自动切 seed 快照、100% 抛 QuotaExhaustedError；
  * 批量算路的配额按【最终路线数】计，不是按请求数。

⚠️ 配额是**按天**重置的，跨日自动开新账（key = 本地日期）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .errors import QuotaExhaustedError


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


@dataclass
class ServiceQuota:
    service: str
    limit: int
    used: int = 0
    reserved: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used - self.reserved)

    @property
    def ratio(self) -> float:
        return (self.used + self.reserved) / self.limit if self.limit else 0.0

    @property
    def level(self) -> str:
        r = self.ratio
        if r >= 1.0:
            return "exhausted"
        if r >= 0.95:
            return "red"
        if r >= 0.80:
            return "yellow"
        return "green"


@dataclass
class QuotaLedger:
    """按服务分别记账。默认服务：place / route / geocode / geoconv。"""

    db_path: str | None = None
    limits: dict[str, int] = field(default_factory=lambda: {
        "place": 100, "route": 5000, "geocode": 5000, "geoconv": 5000, "directionlite": 5000,
    })
    day: str = field(default_factory=today)

    def __post_init__(self) -> None:
        self._lock = threading.RLock()
        self._q: dict[str, ServiceQuota] = {
            s: ServiceQuota(service=s, limit=int(l)) for s, l in self.limits.items()
        }
        self._conn: sqlite3.Connection | None = None
        if self.db_path:
            if self.db_path == ":memory:":
                self._conn = sqlite3.connect(":memory:")
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS quota_usage ("
                    ' day TEXT, service TEXT, used INTEGER, "limit" INTEGER, reserved INTEGER,'
                    " updated_at TEXT, PRIMARY KEY (day, service))"
                )
                self._conn.commit()
                self._load()
                return
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS quota_usage ("
                ' day TEXT, service TEXT, used INTEGER, "limit" INTEGER, reserved INTEGER,'
                " updated_at TEXT, PRIMARY KEY (day, service))"
            )
            self._conn.commit()
            self._load()

    # ---------------- 持久化 ----------------
    def _load(self) -> None:
        assert self._conn is not None
        for row in self._conn.execute(
            'SELECT service, used, "limit" FROM quota_usage WHERE day=?', (self.day,)
        ):
            service, used, limit = row
            q = self._q.setdefault(service, ServiceQuota(service=service, limit=int(limit)))
            q.used = int(used)
            q.limit = int(limit)

    def _persist(self, q: ServiceQuota) -> None:
        if self._conn is None:
            return
        self._conn.execute(
            'INSERT INTO quota_usage (day, service, used, "limit", reserved, updated_at)'
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(day, service) DO UPDATE SET used=excluded.used,"
            ' "limit"=excluded."limit", reserved=excluded.reserved, updated_at=excluded.updated_at',
            (self.day, q.service, q.used, q.limit, q.reserved, datetime.now().isoformat(timespec="seconds")),
        )
        self._conn.commit()

    def _rollover(self) -> None:
        d = today()
        if d != self.day:
            self.day = d

    # ---------------- 预留 / 提交 / 释放 ----------------
    def reserve(self, service: str, n: int) -> None:
        """放行前预占。配额不足立即抛 QuotaExhaustedError（快速失败，绝不重试）。"""
        if n <= 0:
            return
        with self._lock:
            self._rollover()
            q = self._q.get(service)
            if q is None:
                q = self._q[service] = ServiceQuota(service=service, limit=int(self.limits.get(service, 5000)))
            if q.used + q.reserved + n > q.limit:
                raise QuotaExhaustedError(
                    service, -1,
                    f"配额不足：已用 {q.used} + 预占 {q.reserved} + 本次 {n} > 上限 {q.limit}",
                    "快速失败，切 replay / 解析模型",
                )
            q.reserved += n

    def commit(self, service: str, n: int) -> None:
        """调用成功，正式计费。"""
        if n <= 0:
            return
        with self._lock:
            q = self._q.setdefault(service, ServiceQuota(service=service, limit=int(self.limits.get(service, 5000))))
            q.reserved = max(0, q.reserved - n)
            q.used += n
            self._persist(q)

    def release(self, service: str, n: int) -> None:
        """调用失败，归还预占。"""
        if n <= 0:
            return
        with self._lock:
            q = self._q.get(service)
            if q:
                q.reserved = max(0, q.reserved - n)

    def charge(self, service: str, n: int = 1) -> None:
        """reserve + commit 的便捷写法（用于无重试需求的调用）。"""
        self.reserve(service, n)
        self.commit(service, n)

    # ---------------- 查询 ----------------
    def used(self, service: str) -> int:
        with self._lock:
            self._rollover()
            q = self._q.get(service)
            return q.used if q else 0

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return any(q.level == "exhausted" for q in self._q.values())

    def mark_exhausted(self, service: str) -> None:
        """收到 status=302/4 时调用：把账本置满，后续调用立即快速失败。"""
        with self._lock:
            q = self._q.setdefault(service, ServiceQuota(service=service, limit=int(self.limits.get(service, 5000))))
            q.used = q.limit
            q.reserved = 0
            self._persist(q)

    def snapshot(self) -> dict:
        with self._lock:
            self._rollover()
            return {
                "day": self.day,
                "services": {s: {"used": q.used, "limit": q.limit, "reserved": q.reserved, "level": q.level}
                             for s, q in self._q.items()},
                "routes_used": self._q.get("route", ServiceQuota("route", 5000)).used,
                "routes_limit": self._q.get("route", ServiceQuota("route", 5000)).limit,
                "poi_used": self._q.get("place", ServiceQuota("place", 100)).used,
                "poi_limit": self._q.get("place", ServiceQuota("place", 100)).limit,
            }

    def as_json(self) -> str:
        return json.dumps(self.snapshot(), ensure_ascii=False, indent=2)

    def close(self) -> None:
        """释放 SQLite 句柄（Windows 下不关会导致临时目录删不掉）。"""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def reset(self, service: str | None = None) -> None:
        """测试用：清零。"""
        with self._lock:
            for s, q in self._q.items():
                if service in (None, s):
                    q.used = q.reserved = 0
                    self._persist(q)
