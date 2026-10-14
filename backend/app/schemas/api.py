"""API 层通用响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"
    data_mode: str
    llm: bool = False
    seed_available: bool = False
    seed_community: str | None = None
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
