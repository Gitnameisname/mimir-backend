"""
Reranker 추상 인터페이스 — Phase 2 FG2.2

Retriever 결과(후보군)를 입력받아 더 정확한 순서로 재정렬하고
상위 top_k개만 반환한다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from app.services.retrieval.base import RetrievalResult

# Reranker에 입력할 최대 후보 수 (CPU 비용 제한)
MAX_CANDIDATES = 100


class Reranker(ABC):
    """재정렬 전략 추상 인터페이스."""

    @abstractmethod
    async def rerank(
        self,
        query: str,
        candidates: List[RetrievalResult],
        top_k: int = 10,
    ) -> List[RetrievalResult]:
        """후보 결과를 재정렬하여 상위 top_k개를 반환한다.

        Args:
            query: 원본 검색 쿼리
            candidates: Retriever가 반환한 후보 리스트 (최대 MAX_CANDIDATES개 처리)
            top_k: 반환할 최대 결과 수

        Returns:
            재정렬된 RetrievalResult 리스트 (최대 top_k개)
        """

    def _truncate_candidates(
        self,
        candidates: List[RetrievalResult],
    ) -> List[RetrievalResult]:
        """입력 후보를 MAX_CANDIDATES 이내로 제한한다."""
        import logging
        if len(candidates) > MAX_CANDIDATES:
            logging.getLogger(__name__).warning(
                "%s: candidates %d > MAX_CANDIDATES %d — truncating",
                self.__class__.__name__,
                len(candidates),
                MAX_CANDIDATES,
            )
            return candidates[:MAX_CANDIDATES]
        return candidates
