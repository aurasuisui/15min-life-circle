"""pytest 全局配置：让后端单测**默认零网络、零配额**。

  * `DATA_MODE=mock`：默认走确定性合成 provider；
  * `LLM_ENABLED=false`：单测不打真实 LLM（否则每次 7~15 秒、且依赖外网）；
    AI 层自身的单测在 `app/ai/tests/` 里，它们会自己开 LLM 或 mock。
  * `SEED_DIR`：指向仓库内的 data/seed。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ.setdefault("DATA_MODE", "mock")
os.environ.setdefault("SEED_DIR", str(ROOT / "data" / "seed"))
os.environ.setdefault("SEED_COMMUNITY", "zhangjiang-haike")
os.environ.setdefault("LLM_ENABLED", "false")
os.environ.setdefault("STRICT_SEED", "false")
