"""
Hybrid Retriever — FTS + Vector → RRF(k=60) (Phase 2 FG2.2)

두 Retriever를 병렬 실행하고 Reciprocal Rank Fusion으로 통합.
하나가 실패해도 나머지 결과로 응답을 반환한다.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from app.services.retrieval.base import Retriever, RetrievalResult
from app.services.retrieval.fts_retriever import FTSRetriever
from app.services.retrieval.vector_retriever import VectorRetriever

logger = logging.getLogger(__name__)

_RRF_K = 60  # RRF 상수 (표준값, 테스트에서 직접 참조 가능)


class HybridRetriever(Retriever):
    """FTS와 Vector 검색을 RRF로 통합하는 Hybrid Retriever."""

    def __init__(
        self,
        fts: FTSRetriever,
        vector: VectorRetriever,
        fts_weight: float = 0.4,
        vector_weight: float = 0.6,
    ) -> None:
        self._fts = fts
        self._vector = vector
        self._fts_weight = fts_weight
        self._vector_weight = vector_weight

    async def retrieve(
        self,
        query: str,
        document_type: str,
        top_k: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[RetrievalResult]:
        self._warn_if_no_acl(filters)

        # FTS + Vector 병렬 실행, 하나 실패해도 나머지 사용
        fts_task = asyncio.create_task(
            self._fts.retrieve(query, document_type, top_k * 3, filters)
        )
        vec_task = asyncio.create_task(
            self._vector.retrieve(query, document_type, top_k * 3, filters)
        )

        fts_results, vec_results = await asyncio.gather(
            fts_task, vec_task, return_exceptions=True
        )

        if isinstance(fts_results, Exception):
            logger.warning("FTSRetriever failed in HybridRetriever: %s", fts_results)
            fts_results = []
        if isinstance(vec_results, Exception):
            logger.warning("VectorRetriever failed in HybridRetriever: %s", vec_results)
            vec_results = []

        return self._rrf_merge(fts_results, vec_results, top_k)

    def _rrf_merge(
        self,
        fts_results: List[RetrievalResult],
        vec_results: List[RetrievalResult],
        top_k: int,
    ) -> List[RetrievalResult]:
        """Reciprocal Rank Fusion으로 두 리스트를 통합한다."""
        scores: Dict[str, float] = {}
        result_map: Dict[str, RetrievalResult] = {}

        def _key(r: RetrievalResult) -> str:
            return f"{r.document_id}:{r.node_id}"

        for rank, result in enumerate(fts_results, start=1):
            k = _key(result)
            scores[k] = scores.get(k, 0.0) + self._fts_weight / (_RRF_K + rank)
            result_map[k] = result

        for rank, result in enumerate(vec_results, start=1):
            k = _key(result)
            scores[k] = scores.get(k, 0.0) + self._vector_weight / (_RRF_K + rank)
            if k not in result_map:
                result_map[k] = result

        sorted_keys = sorted(scores, key=lambda k: scores[k], reverse=True)[:top_k]

        merged = []
        for k in sorted_keys:
            r = result_map[k]
            # RRF 통합 스코어로 교체
            merged.append(
                RetrievalResult(
                    document_id=r.document_id,
                    version_id=r.version_id,
                    node_id=r.node_id,
                    content=r.content,
                    score=scores[k],
                    citation=r.citation,
                    metadata=r.metadata,
                    document_type=r.document_type,
                    document_title=r.document_title,
                    chunk_id=r.chunk_id,
                )
            )
        return merged
