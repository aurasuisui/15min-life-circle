"""测试公共工具：零网络 provider 装配 + 确定性伪随机。

⚠️ 本文件不引入任何第三方依赖，保证 `scripts/selftest.py` 的 pytest shim 也能跑。

注意（可选依赖）：`backend/tests/` 下若存在依赖 pytest 高级特性（fixture / monkeypatch）
的用例，请用 `pytest` 运行；本目录其余用例均可在无第三方依赖下用
`python scripts/selftest.py` 跑通。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

SEED_DIR = BACKEND.parent / "data" / "seed"

#: 在**任何 TestClient / provider 被构造之前**就把默认值钉死：
#:   * 单测不打真实 LLM（每次 7~15 秒、依赖外网）
#:   * 默认 mock 模式（零网络、零配额）
#: pytest 走 backend/conftest.py，scripts/selftest.py 走本文件，两条路径都覆盖。
os.environ.setdefault("LLM_ENABLED", "false")
os.environ.setdefault("DATA_MODE", "mock")
os.environ.setdefault("SEED_DIR", str(SEED_DIR))
os.environ.setdefault("SEED_COMMUNITY", "zhangjiang-haike")


def use_mode(mode: str, community: str | None = None):
    """切换数据模式并重置 provider 注册表（测试之间互不污染）。"""
    os.environ["DATA_MODE"] = mode
    os.environ["SEED_DIR"] = str(SEED_DIR)
    if community:
        os.environ["SEED_COMMUNITY"] = community
    os.environ["STRICT_SEED"] = "false"
    from app.providers import registry

    registry.reset_state()
    return registry


def lcg(seed: int = 1):
    """确定性伪随机（不用 numpy.random，全平台可复现）。"""
    state = [seed & 0xFFFFFFFF]

    def nxt() -> float:
        state[0] = (1103515245 * state[0] + 12345) & 0x7FFFFFFF
        return state[0] / 0x7FFFFFFF

    return nxt
