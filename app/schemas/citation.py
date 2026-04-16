"""
Citation 5-tuple 스키마 — Phase 2 FG2.1

검색 결과의 근거를 검증 가능한 좌표로 표준화한다.

S1 rag.Citation (index, chunk_id, ...)과 이름이 같지만 별도 모듈이므로 충돌 없음.
S2 클라이언트는 이 모듈을 사용하고, S1 클라이언트는 schemas.rag.Citation을 그대로 사용.
"""
from __future__ import annotations

import hashlib
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class Citation(BaseModel):
    """검색 결과의 검증 가능한 위치 좌표 (5-tuple).

    Attributes:
        document_id: 근거 문서 UUID
        version_id: 인용 시점의 문서 버전 UUID (개정 후에도 원본 확인 가능)
        node_id: 문서 내 섹션 UUID (위치 추적)
        span_offset: 청크 내 문자 오프셋 — 단락 단위 인용 시 None
        content_hash: SHA-256(청크 원문) — 내용 변조 감지용
    """

    document_id: UUID
    version_id: UUID
    node_id: UUID
    span_offset: Optional[int] = Field(None, ge=0)
    content_hash: str = Field(..., min_length=64, max_length=64)

    model_config = {"frozen": True}

    @classmethod
    def from_chunk(
        cls,
        document_id: "str | UUID",
        version_id: "str | UUID",
        node_id: "str | UUID",
        source_text: str,
        span_offset: Optional[int] = None,
    ) -> "Citation":
        """청크 메타데이터로부터 Citation을 생성한다.

        Args:
            document_id: 문서 UUID (str 또는 UUID)
            version_id: 버전 UUID (str 또는 UUID)
            node_id: 노드 UUID (str 또는 UUID)
            source_text: 청크 원문 (SHA-256 계산 대상)
            span_offset: 청크 내 문자 오프셋 (단락 단위 인용 시 None)

        Returns:
            Citation 5-tuple 객체

        Raises:
            ValueError: source_text가 빈 문자열인 경우
        """
        if not source_text:
            raise ValueError("source_text must not be empty")
        content_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        return cls(
            document_id=UUID(str(document_id)),
            version_id=UUID(str(version_id)),
            node_id=UUID(str(node_id)),
            span_offset=span_offset,
            content_hash=content_hash,
        )

    def verify(self, source_text: str) -> bool:
        """source_text의 SHA-256이 content_hash와 일치하는지 검증한다.

        Args:
            source_text: 검증할 원문 텍스트

        Returns:
            일치하면 True, 내용이 변조됐으면 False
        """
        return (
            hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            == self.content_hash
        )


class CitationVerifyResponse(BaseModel):
    """Citation 검증 응답."""

    verified: bool
    original_text: Optional[str] = None  # 원문 (권한 있을 때만 포함)
    modified: bool = False               # 내용 변경 여부
    citation: Citation                   # 요청받은 Citation echo


class CitationContentResponse(BaseModel):
    """Citation 원문 조회 응답."""

    content: str
    metadata: dict
    citation: Citation
