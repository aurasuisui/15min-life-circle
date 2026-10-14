"""百度地图 Web 服务 API 客户端层。

- sign.py       SN 签名（复用 scripts/check_ak.py 里已验证的实现）
- ratelimit.py  按服务分桶的令牌桶（2.5 QPS，官方 3 QPS 留 17% 余量）
- quota.py      QuotaLedger 每日配额账本（SQLite 落盘，支持预留/提交/释放）
- cache.py      文件缓存（参数哈希键 + TTL，命中不消耗配额）
- errors.py     按服务区分的错误分类器（CONTRACT §3.3）
- client.py     BaiduClient：geocode / place_around / walking_route
"""
from __future__ import annotations

from .cache import FileCache
from .client import BaiduClient, EndpointDegraded, get_client
from .errors import (
    BaiduApiError,
    InvalidAkError,
    ServiceDisabledError,
    SnVerifyError,
    TransientApiError,
    classify,
)
from .quota import QuotaExhaustedError, QuotaLedger
from .sign import bd_encode, make_sn, verify_sign_vector

__all__ = [
    "BaiduClient",
    "BaiduApiError",
    "EndpointDegraded",
    "FileCache",
    "InvalidAkError",
    "QuotaExhaustedError",
    "QuotaLedger",
    "ServiceDisabledError",
    "SnVerifyError",
    "TransientApiError",
    "bd_encode",
    "classify",
    "get_client",
    "make_sn",
    "verify_sign_vector",
]
