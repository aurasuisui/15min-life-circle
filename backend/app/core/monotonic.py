"""PAVA 保序回归：把「耗时随距离单调不减」这一物理先验施加到采样序列上。

为什么需要：百度返回的绑路结果会抖动，偶尔出现 t(r2) < t(r1)（r2 > r1），
这是算法噪声而非真实可达性。直接用会导致射线求根出现假区间。

同时把「被修正的样本比例」作为一个数据质量指标输出（方案目标 < 15%）。
"""
from __future__ import annotations


def pava(values: list[float], weights: list[float] | None = None) -> list[float]:
    """Pool Adjacent Violators Algorithm，返回非降序列。"""
    n = len(values)
    if n == 0:
        return []
    w = list(weights) if weights else [1.0] * n
    vals = list(values)
    # 每个块记录 [值, 权重, 元素个数]
    blocks: list[list[float]] = []
    for i in range(n):
        blocks.append([vals[i], w[i], 1])
        while len(blocks) >= 2 and blocks[-2][0] > blocks[-1][0]:
            v2, w2, n2 = blocks.pop()
            v1, w1, n1 = blocks.pop()
            tw = w1 + w2
            blocks.append([(v1 * w1 + v2 * w2) / tw, tw, n1 + n2])

    out: list[float] = []
    for v, _w, cnt in blocks:
        out.extend([v] * int(cnt))
    return out


def monotonicity_stats(raw: list[float]) -> dict:
    """返回修正后的序列与质量指标。"""
    if not raw:
        return {"fitted": [], "modified_ratio": 0.0, "n": 0}
    fitted = pava(raw)
    modified = sum(1 for a, b in zip(raw, fitted) if abs(a - b) > 1e-9)
    return {
        "fitted": fitted,
        "modified_ratio": modified / len(raw),
        "n": len(raw),
    }
