"""全局配置：从 .env 读取，AK/SK 绝不硬编码。"""
from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # ---- 百度地图 ----
    baidu_map_server_ak: str = ""
    baidu_map_server_sk: str = ""
    baidu_map_browser_ak: str = ""

    # ---- 配额保护（个人开发者：3 QPS）----
    api_qps_limit: float = 2.5
    api_daily_poi_limit: int = 100
    api_daily_route_limit: int = 5000

    # ---- 运行模式 ----
    # seed = 只读 data/seed 快照（零配额，评审方可直接跑）
    # live = 实时调用百度地图 API
    # hybrid = 优先读缓存，未命中才实时调用
    data_mode: str = "seed"
    enable_offline_replay: bool = True

    cache_dir: str = str(ROOT / "data" / "cache")
    seed_dir: str = str(ROOT / "data" / "seed")

    @property
    def is_live(self) -> bool:
        return self.data_mode in ("live", "hybrid")

    @property
    def has_ak(self) -> bool:
        return bool(self.baidu_map_server_ak)


settings = Settings()
