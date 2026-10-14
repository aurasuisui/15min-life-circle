"""统一错误响应：`{"error": {"code": "...", "message": "..."}}`（CONTRACT §5）。"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..baidu.client import EndpointDegraded
from ..baidu.errors import (
    BaiduApiError,
    InvalidAkError,
    IpWhitelistError,
    ParameterError,
    QpsLimitedError,
    QuotaExhaustedError,
    ServiceDisabledError,
    SnVerifyError,
    TransientApiError,
)
from ..providers.base import SeedMissingError

log = logging.getLogger(__name__)


def error_payload(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _status_for(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, RequestValidationError):
        return 422, "INVALID_PARAMETER"
    if isinstance(exc, ParameterError):
        return 400, "INVALID_PARAMETER"
    if isinstance(exc, QuotaExhaustedError):
        return 503, "QUOTA_EXHAUSTED"
    if isinstance(exc, SeedMissingError):
        return 503, "SEED_MISSING"
    if isinstance(exc, EndpointDegraded):
        return 503, "ENDPOINT_DEGRADED"
    if isinstance(exc, (InvalidAkError, IpWhitelistError, SnVerifyError, ServiceDisabledError)):
        return 502, "UPSTREAM_AUTH"
    if isinstance(exc, QpsLimitedError):
        return 503, "UPSTREAM_QPS_LIMITED"
    if isinstance(exc, TransientApiError):
        return 502, "UPSTREAM_TRANSIENT"
    if isinstance(exc, BaiduApiError):
        return 502, "UPSTREAM_ERROR"
    if isinstance(exc, ValueError):
        return 400, "INVALID_PARAMETER"
    return 500, "INTERNAL_ERROR"


def install(app: FastAPI) -> None:
    async def _unhandled(request: Request, exc: Exception):  # noqa: ANN001
        status, code = _status_for(exc)
        log.exception("请求 %s 处理失败（%s）", request.url.path, code)
        return JSONResponse(status_code=status, content=error_payload(code, str(exc)))

    async def _validation(request: Request, exc: Exception):  # noqa: ANN001
        detail = getattr(exc, "errors", lambda: str(exc))()
        return JSONResponse(status_code=422,
                            content=error_payload("INVALID_PARAMETER", str(detail[:3])))

    # ⚠️ 顺序很关键：Starlette 按「异常类的 MRO」匹配 handler，
    # RequestValidationError 是 ValueError 的子类，必须先注册更具体的那个。
    app.add_exception_handler(RequestValidationError, _validation)  # type: ignore[arg-type]
    for exc_type in (BaiduApiError, SeedMissingError, EndpointDegraded, ValueError):
        app.add_exception_handler(exc_type, _unhandled)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled)
