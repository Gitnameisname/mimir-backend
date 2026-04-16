"""
RAG (Retrieval-Augmented Generation) API 스키마.

Phase 11: 문서 기반 자연어 질의응답 시스템.
"""

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _validate_uuid_field(value: Optional[str], field_name: str) -> Optional[str]:
    """UUID 형식 검증 헬퍼."""
    if value is not None and not _UUID_RE.match(value):
        raise ValueError(f"{field_name}은(는) 유효한 UUID 형식이어야 합니다.")
    return value


# ---------------------------------------------------------------------------
# 공통 타입
# ---------------------------------------------------------------------------

class Citation(BaseModel):
    """응답 근거 — 청크와 원본 노드 링크 매핑."""
    index: int                          # [1], [2] 형식의 번호
    chunk_id: str
    document_id: str
    document_title: Optional[str] = None
    node_id: Optional[str] = None
    node_path: list[str] = Field(default_factory=list)
    source_text: str                    # 근거 청크 텍스트 (요약)
    similarity: float = 0.0


class RetrievedChunk(BaseModel):
    """Retriever가 반환한 청크 요약 (응답 메타데이터 포함)."""
    chunk_id: str
    document_id: str
    document_title: Optional[str] = None
    node_id: Optional[str] = None
    source_text: str
    similarity: float
    chunk_index: int


# ---------------------------------------------------------------------------
# Conversation (대화 세션)
# ---------------------------------------------------------------------------

class ConversationCreate(BaseModel):
    title: Optional[str] = None
    document_id: Optional[str] = None  # 문서 컨텍스트 고정 (optional)

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, v: Optional[str]) -> Optional[str]:
        return _validate_uuid_field(v, "document_id")


class ConversationResponse(BaseModel):
    id: str
    user_id: str
    title: Optional[str] = None
    document_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(BaseModel):
    conversations: list[ConversationResponse]
    total: int


# ---------------------------------------------------------------------------
# RAG Message
# ---------------------------------------------------------------------------

class MessageResponse(BaseModel):
    id: str
    conversation_id: str
    role: str                           # "user" | "assistant"
    content: str
    citations: list[Citation] = Field(default_factory=list)
    context_chunks: list[RetrievedChunk] = Field(default_factory=list)
    token_used: Optional[int] = None
    model: Optional[str] = None
    created_at: datetime


class ConversationDetailResponse(BaseModel):
    conversation: ConversationResponse
    messages: list[MessageResponse]


# ---------------------------------------------------------------------------
# RAG Query (단건 질의)
# ---------------------------------------------------------------------------

class RAGQueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    conversation_id: Optional[str] = None  # 대화 이력 연속 시 필수
    document_id: Optional[str] = None      # 특정 문서 범위 제한 (optional)
    document_type: Optional[str] = None    # Phase 12: 타입별 RAG 플러그인 선택
    stream: bool = True                     # SSE 스트리밍 여부

    @field_validator("conversation_id")
    @classmethod
    def validate_conversation_id(cls, v: Optional[str]) -> Optional[str]:
        return _validate_uuid_field(v, "conversation_id")

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, v: Optional[str]) -> Optional[str]:
        return _validate_uuid_field(v, "document_id")


class RAGQueryResponse(BaseModel):
    """단건 RAG 질의 응답 (비스트리밍)."""
    answer: str
    citations: list[Citation]
    context_chunks: list[RetrievedChunk]
    conversation_id: str
    message_id: str
    model: str
    token_used: int


# ---------------------------------------------------------------------------
# SSE 스트리밍 이벤트 타입
# ---------------------------------------------------------------------------

class SSEEvent(BaseModel):
    """SSE 스트리밍 이벤트."""
    event: str      # "start" | "delta" | "citation" | "done" | "error"
    data: dict


# ---------------------------------------------------------------------------
# S2 Phase 2: 멀티턴 RAG 스키마 (S1 하위호환)
# ---------------------------------------------------------------------------

from typing import List
from uuid import UUID
from app.schemas.citation import Citation as Citation5Tuple


class RAGRequest(BaseModel):
    """RAG 질의 요청 — S2 멀티턴 지원.

    S1 하위호환: conversation_id 없으면 단발 쿼리 모드.
    S2 확장: conversation_id 있으면 멀티턴 모드 (QueryRewriter, Citation 캐시 활성화).
    """
    query: str = Field(..., min_length=1, max_length=2000, description="사용자 질의")
    top_k: int = Field(10, ge=1, le=50, description="검색 결과 수")
    document_type: Optional[str] = Field(None, description="검색 대상 DocumentType")
    conversation_id: Optional[UUID] = Field(
        None,
        description="멀티턴 대화 ID — 제공 시 멀티턴 모드 활성화",
    )


class RAGCitationInfo(BaseModel):
    """응답 내 단일 Citation 정보 (S2 Citation 5-tuple 포함)."""
    index: int
    citation: Citation5Tuple
    snippet: str


class RAGResponse(BaseModel):
    """RAG 질의 응답 — S2 멀티턴 필드 포함.

    S1 하위호환: rewritten_query, context_compressed, turn_number는 S1 클라이언트가 무시.
    """
    answer: str
    citations: List[RAGCitationInfo] = Field(default_factory=list)
    rewritten_query: Optional[str] = Field(
        None,
        description="재작성된 쿼리 (멀티턴 모드에서만 제공, 투명성)",
    )
    context_compressed: bool = Field(False, description="대화 요약 사용 여부")
    turn_number: int = Field(1, description="현재 대화 턴 번호")
