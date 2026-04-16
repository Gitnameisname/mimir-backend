"""
Conversation 도메인 Pydantic 스키마 — Phase 3 S2.

Phase 2 최소 정의(ConversationMessage, MessageRole)를 유지하면서
Phase 3 API 요청/응답 스키마를 추가한다.

응답 envelope: success_response() / list_response() (기존 프로젝트 규약)
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Phase 2 최소 정의 (하위호환 유지)
# ---------------------------------------------------------------------------

class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ConversationMessage(BaseModel):
    """단일 대화 메시지 (Phase 2 호환)."""

    role: MessageRole
    content: str
    turn_number: int = 0


# ---------------------------------------------------------------------------
# Phase 3 응답 스키마
# ---------------------------------------------------------------------------

class MessageOut(BaseModel):
    """Message 응답 스키마."""

    id: str
    turn_id: str
    role: str
    content: str
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class TurnOut(BaseModel):
    """Turn 응답 스키마."""

    id: str
    conversation_id: str
    turn_number: int
    created_at: datetime
    user_message: str
    assistant_response: str
    retrieval_metadata: dict[str, Any] = Field(default_factory=dict)
    messages: list[MessageOut] = Field(default_factory=list)


class ConversationOut(BaseModel):
    """Conversation 요약 응답 스키마."""

    id: str
    owner_id: str
    organization_id: str
    title: str
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    retention_days: int
    expires_at: Optional[datetime] = None
    access_level: str
    created_at: datetime
    updated_at: datetime


class ConversationDetailOut(ConversationOut):
    """Conversation 상세 응답 스키마 (turns 포함)."""

    turns: list[TurnOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 3 요청 스키마
# ---------------------------------------------------------------------------

class ConversationCreateRequest(BaseModel):
    """대화 생성 요청."""

    title: str = Field(..., min_length=1, max_length=256)
    metadata: Optional[dict[str, Any]] = None
    retention_days: Optional[int] = Field(None, ge=1, le=3650)
    access_level: str = Field("private")

    @field_validator("access_level")
    @classmethod
    def validate_access_level(cls, v: str) -> str:
        allowed = {"private", "organization", "public"}
        if v not in allowed:
            raise ValueError(f"access_level must be one of {allowed}")
        return v


class ConversationUpdateRequest(BaseModel):
    """대화 수정 요청."""

    title: Optional[str] = Field(None, min_length=1, max_length=256)
    metadata: Optional[dict[str, Any]] = None
    status: Optional[str] = None
    access_level: Optional[str] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in {"active", "archived"}:
            raise ValueError("status must be 'active' or 'archived'")
        return v

    @field_validator("access_level")
    @classmethod
    def validate_access_level(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in {"private", "organization", "public"}:
            raise ValueError("access_level must be private, organization, or public")
        return v


class RedactRequest(BaseModel):
    """민감 정보 제거(redact) 요청."""

    fields: list[str] = Field(..., min_length=1)
    reason: str = Field(..., min_length=1, max_length=500)

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, v: list[str]) -> list[str]:
        allowed = {"user_message", "assistant_response"}
        invalid = set(v) - allowed
        if invalid:
            raise ValueError(f"Invalid fields: {invalid}. Allowed: {allowed}")
        return v
