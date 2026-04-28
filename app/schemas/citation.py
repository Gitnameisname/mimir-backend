"""
Citation 5-tuple 스키마 — Phase 2 FG2.1 / S3 Phase 4 FG 4-3 갱신.

검색 결과의 근거를 검증 가능한 좌표로 표준화한다.

S1 rag.Citation (index, chunk_id, ...)과 이름이 같지만 별도 모듈이므로 충돌 없음.
S2 클라이언트는 이 모듈을 사용하고, S1 클라이언트는 schemas.rag.Citation을 그대로 사용.

S3 Phase 4 FG 4-3 (2026-04-28): ``citation_basis`` 필드 신설 — node_content vs rendered_text 분기.
Disagreement Record (`docs/disagreements/2026-04-28-fg43-citations-table-absence.md`):
``citations`` 테이블 부재로 작업지시서의 Alembic 마이그레이션은 미적용. 본 모델만 갱신.
"""
from __future__ import annotations

import hashlib
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


CitationBasis = Literal["node_content", "rendered_text"]
"""Citation 의 정본 텍스트 원천.

- ``node_content``: 청크 / 노드 본문 (``document_chunks.source_text``) 기준
- ``rendered_text``: 렌더링된 텍스트 (FG 4-2 ``read_document_render``) 기준
"""


class Citation(BaseModel):
    """검색 결과의 검증 가능한 위치 좌표 (5-tuple + basis).

    Attributes:
        document_id: 근거 문서 UUID
        version_id: 인용 시점의 문서 버전 UUID (개정 후에도 원본 확인 가능)
        node_id: 문서 내 섹션 UUID (위치 추적)
        span_offset: 청크 내 문자 오프셋 — 단락 단위 인용 시 None
        content_hash: SHA-256(청크 원문) — 내용 변조 감지용
        citation_basis: 정본 텍스트 종류 (S3 Phase 4 FG 4-3 신설). default ``node_content``.
    """

    document_id: UUID
    version_id: UUID
    node_id: UUID
    span_offset: Optional[int] = Field(None, ge=0)
    content_hash: str = Field(..., min_length=64, max_length=64)
    citation_basis: CitationBasis = "node_content"

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
