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


class ScopeProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    organization_id: Optional[str] = None


class ScopeProfileUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None


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
