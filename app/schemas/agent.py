"""
Agent / ScopeProfile Pydantic 스키마 — Phase 4 (S2).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# ScopeProfile 스키마
# ---------------------------------------------------------------------------

class FilterConditionSchema(BaseModel):
    field: str
    op: str = Field(description="eq | neq | in | not_in | contains")
    value: Any


class FilterExpressionSchema(BaseModel):
    and_: list[FilterConditionSchema] = Field(default_factory=list, alias="and")
    or_: list[FilterConditionSchema] = Field(default_factory=list, alias="or")

    model_config = {"populate_by_name": True}


class ScopeDefinitionSchema(BaseModel):
    id: str
    scope_profile_id: str
    scope_name: str
    description: Optional[str] = None
    acl_filter: dict = Field(default_factory=dict)
    created_at: datetime


class ScopeProfileResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    organization_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    scopes: list[ScopeDefinitionSchema] = Field(default_factory=list)
    # S3 Phase 3 FG 3-2 (2026-04-27): 운영 설정 (viewers 노출 등)
    settings: "ScopeProfileSettingsSchema" = Field(
        default_factory=lambda: ScopeProfileSettingsSchema()
    )
    # S3 Phase 4 FG 4-0 §2.1.6 (2026-04-28): MCP tool-level ACL 화이트리스트
    allowed_tools: list[str] = Field(
        default_factory=list,
        description=(
            "에이전트가 호출 가능한 MCP 도구 화이트리스트. "
            "빈 배열 = default-deny (모든 도구 거부). "
            "등록 가능: app.schemas.mcp.known_tool_names()"
        ),
    )


# S3 Phase 3 FG 3-2 (2026-04-27)
class ScopeProfileSettingsSchema(BaseModel):
    """ScopeProfile 운영 설정 schema (request / response 양방향)."""

    expose_viewers: bool = Field(
        default=False,
        description=(
            "Contributors 패널 (FG 3-1) 의 viewers 섹션 노출 정책. "
            "False (기본) 면 정책 게이트가 강제 false 반환."
        ),
    )


class ScopeProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    organization_id: Optional[str] = None
    # S3 Phase 3 FG 3-2 (2026-04-27): 신규 profile 생성 시 settings 지정 (옵셔널)
    settings: Optional[ScopeProfileSettingsSchema] = Field(
        default=None,
        description="운영 설정. 미지정 시 모든 키 default(보수적)로 채워짐.",
    )
    # S3 Phase 4 FG 4-0 §2.1.6 (2026-04-28): 신규 profile 생성 시 allowed_tools 지정 (옵셔널)
    allowed_tools: Optional[list[str]] = Field(
        default=None,
        description=(
            "MCP tool 호출 화이트리스트. 미지정 / 빈 배열 = default-deny. "
            "known_tool_names() 외 도구 이름은 422."
        ),
    )


class ScopeProfileUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    # S3 Phase 3 FG 3-2 (2026-04-27): settings PATCH (옵셔널). dataclass 필드만 적용.
    settings: Optional[ScopeProfileSettingsSchema] = Field(
        default=None,
        description="settings 부분 갱신. 명시된 키만 반영, 미지의 키는 raw 보존.",
    )
    # S3 Phase 4 FG 4-0 §2.1.6 (2026-04-28): allowed_tools 전체 교체 (PUT 시맨틱)
    allowed_tools: Optional[list[str]] = Field(
        default=None,
        description=(
            "MCP tool 화이트리스트 전체 교체. None = 미수정. "
            "빈 배열 [] = default-deny 로 재설정. "
            "known_tool_names() 외 도구 이름은 422."
        ),
    )


class ScopeDefinitionCreate(BaseModel):
    scope_name: str = Field(min_length=1, max_length=100)
    description: Optional[str] = None
    acl_filter: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Agent 스키마
# ---------------------------------------------------------------------------

class AgentResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    organization_id: Optional[str] = None
    scope_profile_id: Optional[str] = None
    is_disabled: bool
    disabled_at: Optional[datetime] = None
    disabled_reason: Optional[str] = None
    metadata: dict = Field(default_factory=dict)
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    organization_id: Optional[str] = None
    scope_profile_id: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class AgentUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    scope_profile_id: Optional[str] = None


class KillSwitchActivate(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500)


class KillSwitchResponse(BaseModel):
    agent_id: str
    is_disabled: bool
    disabled_at: Optional[datetime] = None
    disabled_reason: Optional[str] = None
    message: str


class AgentListResponse(BaseModel):
    items: list[AgentResponse]
    total: int
    limit: int
    offset: int


class ScopeProfileListResponse(BaseModel):
    items: list[ScopeProfileResponse]
    total: int
    limit: int
    offset: int
