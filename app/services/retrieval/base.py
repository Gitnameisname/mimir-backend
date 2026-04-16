"""
Retriever 추상 인터페이스 + RetrievalResult — Phase 2 FG2.2

모든 검색 전략은 Retriever를 상속하고 retrieve()를 구현한다.
반환값 RetrievalResult에는 Citation 5-tuple이 필수 포함된다 (FG2.1 계약).
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.schemas.citation import Citation

logger = logging.getLogger(__name__)


@dataclass
class RetrievalResult:
    """단일 검색 결과.

    Attributes:
        document_id: 문서 UUID
        version_id: 버전 UUID
        node_id: 노드 UUID (nil UUID = node 없음)
        content: 청크 원문
        score: 검색 스코어 (BM25 / cosine / RRF 혼합)
        citation: Citation 5-tuple (필수, FG2.1)
        metadata: 문서/청크 메타데이터
        document_type: DocumentType 이름
        document_title: 문서 제목 (UI 표시용)
        chunk_id: 원본 청크 ID (디버깅용)
    """

    document_id: UUID
    version_id: UUID
    node_id: UUID
    content: str
    score: float
    citation: Citation                          # 필수 — FG2.1 Citation 계약
    metadata: Dict[str, Any] = field(default_factory=dict)
    document_type: str = ""
    document_title: Optional[str] = None
    chunk_id: Optional[str] = None


class Retriever(ABC):
    """검색 전략 추상 인터페이스."""

    @abstractmethod
    async def retrieve(
        self,
        query: str,
        document_type: str,
        top_k: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[RetrievalResult]:
        """쿼리를 검색하여 상위 top_k 결과를 반환한다.

        Args:
            query: 검색 쿼리 문자열
            document_type: DocumentType 이름 (전략 선택, 하드코딩 금지)
            top_k: 반환할 최대 결과 수
            filters: ACL, 날짜 범위 등 추가 필터
                     필수 키: "actor_role" (없으면 공개 문서만 반환)

        Returns:
            RetrievalResult 리스트 (score 내림차순)
        """

    def _warn_if_no_acl(self, filters: Optional[Dict]) -> None:
        """filters에 actor_role 정보가 없으면 경고를 로깅한다."""
        if not filters or "actor_role" not in filters:
            logger.warning(
                "%s.retrieve() called without actor_role filter — "
                "only public documents will be returned",
                self.__class__.__name__,
            )
