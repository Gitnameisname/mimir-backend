"""
FTS Retriever — PostgreSQL Full-Text Search (Phase 2 FG2.2)

S1 search_service.py의 FTS 로직을 Retriever 인터페이스로 이관.
ACL 필터, Citation 생성을 내재화한다.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

import psycopg2.extensions
import psycopg2.extras

from app.services.retrieval.base import Retriever, RetrievalResult
from app.services.retrieval.citation_builder import CitationBuilder, _NIL_NODE_ID

logger = logging.getLogger(__name__)


class FTSRetriever(Retriever):
    """PostgreSQL FTS 기반 키워드 검색 (ts_rank 스코어)."""

    def __init__(self, conn: psycopg2.extensions.connection) -> None:
        self._conn = conn

    async def retrieve(
        self,
        query: str,
        document_type: str,
        top_k: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[RetrievalResult]:
        self._warn_if_no_acl(filters)
        filters = filters or {}
        actor_role = filters.get("actor_role")

        # ACL 조건: is_public 또는 actor_role이 accessible_roles에 포함
        if actor_role:
            acl_cond = "(dc.is_public = TRUE OR %s = ANY(dc.accessible_roles))"
            acl_params: list = [actor_role]
        else:
            acl_cond = "dc.is_public = TRUE"
            acl_params = []

        # document_type 조건: 빈 문자열이면 전체 검색
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
                dc.metadata,
                dc.document_type,
                d.title             AS document_title,
                ts_rank_cd(
                    to_tsvector('simple', coalesce(dc.source_text, '')),
                    plainto_tsquery('simple', %s)
                ) AS score
            FROM document_chunks dc
            JOIN documents d ON dc.document_id = d.id
            WHERE to_tsvector('simple', coalesce(dc.source_text, ''))
                  @@ plainto_tsquery('simple', %s)
              AND dc.is_current = TRUE
              {type_cond}
              AND {acl_cond}
            ORDER BY score DESC
            LIMIT %s
        """
        params = [query, query] + type_params + acl_params + [top_k]

        try:
            with self._conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        except Exception as exc:
            logger.error("FTSRetriever.retrieve failed: %s", exc)
            return []

        results = []
        for row in rows:
            r = self._row_to_result(row)
            if r is not None:
                results.append(r)
        return results

    @staticmethod
    def _row_to_result(row: dict) -> Optional[RetrievalResult]:
        """DB 행을 RetrievalResult로 변환한다. source_text가 없으면 None 반환."""
        source_text = row.get("source_text") or ""
        if not source_text:
            logger.debug(
                "FTSRetriever: skipping chunk %s — empty source_text",
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
            score=float(row["score"] or 0.0),
            citation=citation,
            metadata=dict(row.get("metadata") or {}),
            document_type=row.get("document_type") or "",
            document_title=row.get("document_title"),
            chunk_id=str(row["chunk_id"]) if row.get("chunk_id") else None,
        )
