"""
Vector Retriever — Milvus cosine similarity (Phase 2 FG2.2 / S3 2026-05-11 milvus 전환)

아키텍처 (S2 ⑦ + Phase 10 결정):
  - Milvus 가 벡터 정본 — chunk_id + embedding 만 저장
  - PostgreSQL `document_chunks` 는 메타데이터 + ACL 스냅샷 보관 (embedding 컬럼 없음)
  - 검색 흐름:
      1) query → embedding (`embedding_service.get_embedding_provider`)
      2) Milvus 벡터 유사도 검색 → top_k * 2 후보 (chunk_id, score)
      3) similarity_threshold 1차 필터
      4) PostgreSQL chunk_id IN (...) + delegated ACL + document JOIN
      5) Milvus 점수 매핑 후 score 내림차순 top_k 반환

폐쇄망 대응 (S2 ⑦):
  - MILVUS_HOST 미설정 시 ``_NullMilvusClient`` 가 빈 결과 반환 → 본 retriever 도 빈 list

이전 (변경 전):
  - pgvector ``<=>`` 연산자 + ``dc.embedding`` 컬럼 직접 사용 → 컬럼 부재로 운영에서 실패하던 broken 상태였다.
  - 2026-05-11 본 모듈을 Milvus 정본으로 정리.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

import psycopg2.extensions
import psycopg2.extras

from app.services.retrieval.base import (
    Retriever,
    RetrievalResult,
    build_chunk_acl_clause,
)
from app.services.retrieval.citation_builder import CitationBuilder, _NIL_NODE_ID

logger = logging.getLogger(__name__)


class VectorRetriever(Retriever):
    """Milvus 기반 의미 검색 + PostgreSQL 메타/ACL 조회."""

    def __init__(
        self,
        conn: psycopg2.extensions.connection,
        similarity_threshold: float = 0.3,
    ) -> None:
        self._conn = conn
        self._threshold = similarity_threshold

    async def retrieve(
        self,
        query: str,
        document_type: str,
        top_k: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[RetrievalResult]:
        self._warn_if_no_acl(filters)
        filters = filters or {}

        # 1) query → embedding
        try:
            from app.services.embedding_service import get_embedding_provider
            embedding_provider = get_embedding_provider()
            query_vector = embedding_provider.embed_single(query)
        except Exception as exc:
            logger.error("VectorRetriever: embedding failed: %s — returning empty", exc)
            return []
        if not query_vector or not any(query_vector):
            logger.warning("VectorRetriever: empty query_vector — returning empty")
            return []

        # 2) Milvus 벡터 유사도 검색 — top_k * 2 후보
        try:
            from app.db.milvus import get_milvus
            milvus = get_milvus()
        except Exception as exc:
            logger.error("VectorRetriever: milvus init failed: %s — returning empty", exc)
            return []
        if not milvus.is_available():
            logger.info("VectorRetriever: milvus unavailable — returning empty")
            return []

        try:
            candidates = milvus.search_with_score(query_vector, top_k=top_k * 2)
        except Exception as exc:
            logger.error("VectorRetriever: milvus search failed: %s — returning empty", exc)
            return []
        if not candidates:
            return []

        # 3) similarity threshold 1차 필터 + chunk_id list
        filtered = [(cid, score) for cid, score in candidates if score >= self._threshold]
        if not filtered:
            return []

        chunk_ids = [cid for cid, _ in filtered]
        score_by_chunk: dict[str, float] = {cid: score for cid, score in filtered}

        # 4) PostgreSQL document_chunks 메타 + ACL + document JOIN
        rows = self._fetch_chunk_metadata(chunk_ids, document_type, filters)

        # 5) Milvus 순서대로 RetrievalResult — PG 통과한 것만
        rows_by_chunk: dict[str, dict] = {str(r["chunk_id"]): r for r in rows}
        results: list[RetrievalResult] = []
        for cid, _ in filtered:
            row = rows_by_chunk.get(cid)
            if row is None:
                # ACL / document_type / is_current 필터에서 탈락 → 스킵
                continue
            r = self._row_to_result(row, score=score_by_chunk[cid])
            if r is not None:
                results.append(r)
            if len(results) >= top_k:
                break
        return results

    # ------------------------------------------------------------------
    # PostgreSQL chunk 메타 + ACL 조회
    # ------------------------------------------------------------------

    def _fetch_chunk_metadata(
        self,
        chunk_ids: list[str],
        document_type: str,
        filters: Dict[str, Any],
    ) -> list[dict]:
        if not chunk_ids:
            return []

        acl_cond, acl_params = build_chunk_acl_clause(filters, table_alias="dc")

        if document_type:
            type_cond = "AND dc.document_type = %s"
            type_params: list = [document_type]
        else:
            type_cond = ""
            type_params = []

        sql = f"""
            SELECT
                dc.id               AS chunk_id,
                dc.document_id,
                dc.version_id,
                dc.node_id,
                dc.source_text,
                dc.document_type,
                d.title             AS document_title
            FROM document_chunks dc
            JOIN documents d ON dc.document_id = d.id
            WHERE dc.is_current = TRUE
              AND dc.id = ANY(%s::uuid[])
              {type_cond}
              AND {acl_cond}
        """
        params = [chunk_ids] + type_params + acl_params

        try:
            with self._conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        except Exception as exc:
            logger.error("VectorRetriever._fetch_chunk_metadata failed: %s", exc)
            return []

    @staticmethod
    def _row_to_result(row: dict, score: float = 0.0) -> Optional[RetrievalResult]:
        """DB 행 + Milvus score 를 RetrievalResult 로 변환. source_text 부재 시 None.

        Note (2026-05-11 milvus 전환):
            score 는 Milvus 의 cosine similarity (1=동일, 0=무관). 기존 SQL 의
            ``dc.metadata`` 컬럼은 ``document_chunks`` DDL 에 없으므로 metadata 는
            빈 dict 로 통일.
        """
        source_text = row.get("source_text") or ""
        if not source_text:
            logger.debug(
                "VectorRetriever: skipping chunk %s — empty source_text",
                row.get("chunk_id"),
            )
            return None

        node_id_str = row.get("node_id")
        node_uuid = UUID(str(node_id_str)) if node_id_str else _NIL_NODE_ID
        citation = CitationBuilder.build(
            document_id=row["document_id"],
            version_id=row["version_id"],
            node_id=node_id_str,
            source_text=source_text,
        )
        return RetrievalResult(
            document_id=UUID(str(row["document_id"])),
            version_id=UUID(str(row["version_id"])),
            node_id=node_uuid,
            content=source_text,
            score=float(score),
            citation=citation,
            metadata={},  # document_chunks 에 metadata 컬럼 없음 (Phase 10 결정)
            document_type=row.get("document_type") or "",
            document_title=row.get("document_title"),
            chunk_id=str(row["chunk_id"]) if row.get("chunk_id") else None,
        )
