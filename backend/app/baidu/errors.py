"""百度地图错误分类器（CONTRACT §3.3）。

⚠️ 关键实现约束：不同服务的状态码语义【不一致】，分类器必须拿到 service 上下文，
并同时匹配 message 文本才能可靠区分。本机实测证据：

  | 服务                    | 非法 AK 返回                                        |
  |-------------------------|-----------------------------------------------------|
  | place/v2/search         | {"status":200,"message":"APP不存在，AK有误…"}        |
  | geocoding/v3            | {"status":200,...}                                  |
  | routematrix/v2/walking  | {"status":2,"message":"ak is invalid"}  <- 语义冲突！|

另记：routematrix 在【服务未开通】时返回 status=211（而不是 240）。
"""
from __future__ import annotations

from dataclasses import dataclass

# ---- 语义类别 ----
KIND_OK = "ok"
KIND_QUOTA = "quota"           # 配额耗尽 -> 快速失败，绝不重试
KIND_QPS = "qps"               # 并发超限 -> 抖动退避重试
KIND_TRANSIENT = "transient"   # 瞬时故障（5xx/超时/连接重置）-> 指数退避
KIND_PARAM = "param"           # 参数错误 -> 跳过该点，不重试
KIND_AK = "ak"                 # AK 非法 -> 熔断告警
KIND_SN = "sn"                 # SN 校验失败 -> 熔断告警
KIND_PERMISSION = "permission" # 服务未开通 / 被禁用 -> 降级链
KIND_IP = "ip"                 # IP 白名单失败

RETRYABLE = frozenset({KIND_QPS, KIND_TRANSIENT})


class BaiduApiError(RuntimeError):
    """百度接口业务错误基类。"""

    kind = "unknown"
    retryable = False

    def __init__(self, service: str, status: int, message: str, hint: str = ""):
        self.service = service
        self.status = status
        self.message = message
        self.hint = hint
        super().__init__(f"[{service}] status={status} kind={self.kind} message={message}")


class QuotaExhaustedError(BaiduApiError):
    """当日配额耗尽 —— 快速失败，不重试。"""

    kind = KIND_QUOTA


class TransientApiError(BaiduApiError):
    """瞬时失败（限流 / 网络抖动）—— 指数退避重试。"""

    kind = KIND_TRANSIENT
    retryable = True


class QpsLimitedError(BaiduApiError):
    """QPS 并发超限 —— 降速 + 抖动退避重试。"""

    kind = KIND_QPS
    retryable = True


class InvalidAkError(BaiduApiError):
    """AK 非法 / 不存在 —— 立即熔断，重试是白烧配额。"""

    kind = KIND_AK


class SnVerifyError(BaiduApiError):
    """SN 校验失败 —— 先查签名，再查服务权限。"""

    kind = KIND_SN


class ServiceDisabledError(BaiduApiError):
    """服务未开通 / 被禁用 —— 走降级链。"""

    kind = KIND_PERMISSION


class IpWhitelistError(BaiduApiError):
    """IP 白名单校验失败。"""

    kind = KIND_IP


class ParameterError(BaiduApiError):
    """参数错误 —— 记录并跳过该点，绝不拖垮整批。"""

    kind = KIND_PARAM


@dataclass(frozen=True)
class ErrorClass:
    kind: str
    description: str
    action: str
    retry: bool
    exc: type[BaiduApiError]

    @property
    def retryable(self) -> bool:
        """与 `BaiduApiError.retryable` 保持同名，调用方不必记住两套命名。"""
        return self.retry


#: CONTRACT §3.3 的处置表（kind -> 诊断/处置/是否重试）
ERROR_TABLE: dict[str, ErrorClass] = {
    KIND_QUOTA: ErrorClass(KIND_QUOTA, "配额耗尽", "快速失败，不重试，切 replay/解析模型", False, QuotaExhaustedError),
    KIND_QPS: ErrorClass(KIND_QPS, "QPS 超限", "令牌桶降速 + 抖动退避重试", True, QpsLimitedError),
    KIND_AK: ErrorClass(KIND_AK, "AK 非法", "报错终止并告警", False, InvalidAkError),
    KIND_IP: ErrorClass(KIND_IP, "IP 白名单失败", "报错终止（该 AK 应改用 SN 签名）", False, IpWhitelistError),
    KIND_SN: ErrorClass(KIND_SN, "SN 校验失败或服务未开通", "先查签名，再查权限；不重试", False, SnVerifyError),
    KIND_PERMISSION: ErrorClass(KIND_PERMISSION, "服务被禁用 / AK 类型错", "熔断该服务，走降级链", False, ServiceDisabledError),
    KIND_PARAM: ErrorClass(KIND_PARAM, "参数错误", "记录并跳过该点", False, ParameterError),
    KIND_TRANSIENT: ErrorClass(KIND_TRANSIENT, "瞬时故障", "指数退避 + 全抖动，上限 3 次", True, TransientApiError),
}


def classify(service: str, status: int, message: str = "") -> ErrorClass | None:
    """把 (service, status, message) 三元组映射到错误类别。status==0 返回 None（成功）。

    注意：**不要**写一个全局状态码字典就完事 —— 必须带 service 上下文。
    """
    if status == 0:
        return None
    m = (message or "").lower()
    msg_cn = message or ""

    # ---- 1. 状态码优先（状态码是机器可读的，文本只是补充）----
    if status in (4, 302):
        return ERROR_TABLE[KIND_QUOTA]
    if status == 401:
        return ERROR_TABLE[KIND_QPS]
    if status in (5, 101, 200, 201, 202):
        # 注意：place/geocoding 用 200 表示「APP 不存在」，与 HTTP 200 无关
        return ERROR_TABLE[KIND_AK]
    if status == 210:
        return ERROR_TABLE[KIND_IP]
    if status == 211:
        return ERROR_TABLE[KIND_SN]
    if status in (203, 240, 261):
        return ERROR_TABLE[KIND_PERMISSION]
    if status == 2:
        # ⚠️ 语义冲突：routematrix 的 2 是「ak is invalid」，place 的 2 是「参数错误」。
        # 所以不能只看状态码，必须再匹配 message 文本来区分。
        if "ak is invalid" in m or "ak有误" in msg_cn or "app不存在" in msg_cn:
            return ERROR_TABLE[KIND_AK]
        return ERROR_TABLE[KIND_PARAM]

    # ---- 2. 文本补充（覆盖非标准状态码，如 status=-1 / 未知码）----
    if "ak is invalid" in m or "app不存在" in msg_cn or "ak有误" in msg_cn:
        return ERROR_TABLE[KIND_AK]
    if "白名单" in msg_cn or "whitelist" in m:
        return ERROR_TABLE[KIND_IP]
    if "sn校验失败" in msg_cn or "sn 校验失败" in msg_cn or "sn check" in m:
        return ERROR_TABLE[KIND_SN]
    if "服务被禁用" in msg_cn or "服务未开通" in msg_cn or "权限" in msg_cn or "permission" in m:
        return ERROR_TABLE[KIND_PERMISSION]
    if "quota" in m or "配额" in msg_cn or ("超限" in msg_cn and "并发" not in msg_cn and "qps" not in m):
        return ERROR_TABLE[KIND_QUOTA]
    if "并发" in msg_cn or "qps" in m or "too many requests" in m:
        return ERROR_TABLE[KIND_QPS]
    if "参数" in msg_cn or "param" in m:
        return ERROR_TABLE[KIND_PARAM]
    return ERROR_TABLE[KIND_TRANSIENT]


def to_exception(service: str, status: int, message: str) -> BaiduApiError:
    """构造与分类结果对应的异常实例。"""
    ec = classify(service, status, message)
    if ec is None:
        raise ValueError("status=0 不是错误")
    return ec.exc(service, status, message, ec.action)


def is_quota_message(message: str) -> bool:
    m = (message or "").lower()
    return "quota" in m or "配额" in (message or "")
