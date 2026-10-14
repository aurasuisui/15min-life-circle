"""百度地图 SN 签名（官方《附录 · sn 计算算法》）。

本实现与 scripts/check_ak.py 中的 make_sn 完全一致（已验证官方测试向量通过）。

三个极易踩坑的点（见 docs/SOLUTION.md §2.5）：
  1. 是「value 编码一次 → 整串再编码一次」的双重编码，漏掉第二次是最常见的 211 原因；
  2. path 必须含前导斜杠、结尾斜杠与问号，且不含域名；
  3. sk 直接拼接（不加 &、不加 sk=），且 params 的顺序必须与 URL 中完全一致。
"""
from __future__ import annotations

import hashlib
import urllib.parse

# 官方「编码说明」白名单：非保留字 a-z A-Z 0-9 - _ . ~ 不编码，其余一律编码。
NON_RESERVED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.~"
)

#: 官方测试向量（无需真实 AK 即可验证签名实现）
OFFICIAL_VECTOR_PATH = "/geocoder/v2/"
OFFICIAL_VECTOR_PARAMS = {"address": "百度大厦", "output": "json", "ak": "yourak"}
OFFICIAL_VECTOR_SK = "yoursk"
OFFICIAL_VECTOR_SN = "7de5a22212ffaa9e326444c75a58f9a0"


def bd_encode(value: object) -> str:
    """按百度官方字符表白名单做 UTF-8 URL 编码（空格 -> %20）。"""
    return urllib.parse.quote(str(value), safe="", encoding="utf-8")


def make_sn(path: str, params: dict, sk: str) -> str:
    """计算 SN，32 位小写 MD5。

    Args:
        path: 接口路径，如 `/place/v3/around`（含前导斜杠、不含域名）。
        params: 必须【按 URL 中出现的顺序】排列，且必须包含 ak。
        sk: 服务端私钥。
    """
    query = "&".join(f"{k}={bd_encode(v)}" for k, v in params.items())
    whole = f"{path}?{query}{sk}"
    return hashlib.md5(bd_encode(whole).encode("utf-8")).hexdigest()


def verify_sign_vector() -> bool:
    """用官方测试向量自检签名实现。这是零成本、高价值的回归断言。"""
    return make_sn(OFFICIAL_VECTOR_PATH, OFFICIAL_VECTOR_PARAMS, OFFICIAL_VECTOR_SK) == OFFICIAL_VECTOR_SN
