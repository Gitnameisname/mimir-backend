"""멀티턴 대화 도메인 모델 — Phase 2 최소 정의.

Phase 3 Conversation 도메인에서 확장 예정.
현재는 QueryRewriter / ConversationCompressor에서 사용되는 최소 인터페이스만 정의한다.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ConversationMessage(BaseModel):
    """단일 대화 메시지."""

    role: MessageRole
    content: str
    turn_number: int = 0
