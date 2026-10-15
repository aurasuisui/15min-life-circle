"""API 层通用响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"
    data_mode: str
    llm: bool = False
    seed_available: bool = False
    seed_community: str | None = None
    seed_name: str | None = None
    # 当前激活社区的默认中心（BD09）。前端用它做首屏默认点，
    # 这样切换社区后不需要改前端代码。
    seed_center: dict | None = None
    seed_collected_at: str | None = None
    has_ak: bool = False
    has_sk: bool = False


class GeocodeResponse(BaseModel):
    lat: float
    lng: float
    address: str = ""


class CommunityInfo(BaseModel):
    id: str
    name: str
    center: dict = Field(default_factory=dict)
    address: str = ""
    collected_at: str | None = None


class CommunitiesResponse(BaseModel):
    communities: list[CommunityInfo] = Field(default_factory=list)


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody
