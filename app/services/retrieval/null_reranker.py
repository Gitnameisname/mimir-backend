"""NullReranker — 재정렬 없이 통과 (Phase 2 FG2.2)"""
from __future__ import annotations

from typing import List

from app.services.retrieval.base import RetrievalResult
from app.services.retrieval.reranker_base import Reranker


class NullReranker(Reranker):
    """재정렬 없이 candidates를 그대로 반환한다."""

    async def rerank(
        self,
        query: str,
        candidates: List[RetrievalResult],
        top_k: int = 10,
    ) -> List[RetrievalResult]:
        return candidates[:top_k]
