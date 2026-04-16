"""
RetrieverFactory — 설정 기반 Retriever 동적 생성 (Phase 2 FG2.2)
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import psycopg2.extensions

from app.services.retrieval.base import Retriever


class RetrieverFactory:
    """Retriever 인스턴스를 설정 기반으로 생성한다."""

    @staticmethod
    def create(
        name: str,
        conn: psycopg2.extensions.connection,
        params: Optional[Dict[str, Any]] = None,
    ) -> Retriever:
        """Retriever 이름과 파라미터로 인스턴스를 생성한다.

        Args:
            name: "fts" | "vector" | "hybrid"
            conn: DB 연결
            params: Retriever별 설정 파라미터

        Returns:
            Retriever 구현체

        Raises:
            ValueError: 알 수 없는 Retriever 이름
        """
        from app.services.retrieval.fts_retriever import FTSRetriever
        from app.services.retrieval.vector_retriever import VectorRetriever
        from app.services.retrieval.hybrid_retriever import HybridRetriever

        params = params or {}

        if name == "fts":
            return FTSRetriever(conn)

        if name == "vector":
            return VectorRetriever(
                conn,
                similarity_threshold=params.get("similarity_threshold", 0.3),
            )

        if name == "hybrid":
            fts = FTSRetriever(conn)
            vector = VectorRetriever(
                conn,
                similarity_threshold=params.get("similarity_threshold", 0.3),
            )
            return HybridRetriever(
                fts=fts,
                vector=vector,
                fts_weight=params.get("fts_weight", 0.4),
                vector_weight=params.get("vector_weight", 0.6),
            )

        raise ValueError(
            f"Unknown retriever: {name!r}. Choose from: fts, vector, hybrid"
        )
