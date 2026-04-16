"""
Citation 역참조 서비스 — Phase 2 FG2.1

document_chunks 테이블에서 Citation 5-tuple에 해당하는 청크를 조회하고,
content_hash 일치 여부를 검증한다.

ACL:
  - actor_role을 기반으로 접근 가능한 청크만 반환.
  - 권한 없음 또는 청크 없음 모두 None 반환 (존재 여부 노출 방지).
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional
from uuid import UUID

import psycopg2.extensions
import psycopg2.extras

from app.schemas.citation import (
    Citation,
    CitationContentResponse,
    CitationVerifyResponse,
)
from app.services.retrieval.citation_builder import _NIL_NODE_ID

logger = logging.getLogger(__name__)


class CitationService:
    """Citation 역참조 및 검증 로직."""

    def __init__(self, conn: psycopg2.extensions.connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def verify(
        self,
        document_id: UUID,
        version_id: UUID,
        node_id: UUID,
        content_hash: str,
        actor_role: Optional[str] = None,
        span_offset: Optional[int] = None,
    ) -> Optional[CitationVerifyResponse]:
        """Citation의 content_hash가 현재 DB 내용과 일치하는지 검증한다.

        Args:
            document_id: 문서 UUID
            version_id: 버전 UUID
            node_id: 노드 UUID (nil UUID는 node_id IS NULL 조건으로 처리)
            content_hash: 클라이언트가 저장해둔 SHA-256 hex
            actor_role: 요청 actor 역할 (ACL 필터)
            span_offset: 문자 오프셋 (현재는 메타데이터로만 보관)

        Returns:
            CitationVerifyResponse — 청크를 찾은 경우.
            None — 청크 없음 또는 ACL 위반 (구분 없이 동일 처리).
        """
        chunk = self._fetch_chunk(document_id, version_id, node_id, actor_role)
        if chunk is None:
            return None

        current_hash = hashlib.sha256(
            chunk["source_text"].encode("utf-8")
        ).hexdigest()
        verified = current_hash == content_hash

        citation = Citation(
            document_id=document_id,
            version_id=version_id,
            node_id=node_id,
            span_offset=span_offset,
            content_hash=content_hash,
        )
        return CitationVerifyResponse(
            verified=verified,
            original_text=chunk["source_text"],
            modified=not verified,
            citation=citation,
        )

    def get_content(
        self,
        document_id: UUID,
        version_id: UUID,
        node_id: UUID,
        actor_role: Optional[str] = None,
    ) -> Optional[CitationContentResponse]:
        """Citation에 해당하는 청크 원문과 메타데이터를 반환한다.

        Returns:
            CitationContentResponse — 청크를 찾은 경우.
            None — 청크 없음 또는 ACL 위반.
        """
        chunk = self._fetch_chunk(document_id, version_id, node_id, actor_role)
        if chunk is None:
            return None

        current_hash = hashlib.sha256(
            chunk["source_text"].encode("utf-8")
        ).hexdigest()
        citation = Citation(
            document_id=document_id,
            version_id=version_id,
            node_id=node_id,
            content_hash=current_hash,
        )
        return CitationContentResponse(
            content=chunk["source_text"],
            metadata=dict(chunk.get("metadata") or {}),
            citation=citation,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_chunk(
        self,
        document_id: UUID,
        version_id: UUID,
        node_id: UUID,
        actor_role: Optional[str],
    ) -> Optional[dict]:
        """document_chunks 테이블에서 청크를 조회한다.

        node_id가 nil UUID인 경우 node_id IS NULL 조건으로 조회.
        ACL 필터: is_public 또는 actor_role이 accessible_roles에 포함.
        """
        is_nil_node = node_id == _NIL_NODE_ID

        # ACL 조건 구성
        if actor_role:
            acl_cond = "(dc.is_public = TRUE OR %s = ANY(dc.accessible_roles))"
            acl_params: list = [actor_role]
        else:
            acl_cond = "dc.is_public = TRUE"
            acl_params = []

        # node_id 조건
        if is_nil_node:
            node_cond = "dc.node_id IS NULL"
            node_params: list = []
        else:
            node_cond = "dc.node_id = %s::uuid"
            node_params = [str(node_id)]

        sql = f"""
            SELECT
                dc.source_text,
                dc.metadata
            FROM document_chunks dc
            WHERE dc.document_id = %s::uuid
              AND dc.version_id  = %s::uuid
              AND {node_cond}
              AND {acl_cond}
            LIMIT 1
        """
        params = [str(document_id), str(version_id)] + node_params + acl_params

        try:
            with self._conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
            return dict(row) if row else None
        except Exception as exc:
            logger.error(
                "CitationService._fetch_chunk failed: doc=%s ver=%s node=%s err=%s",
                document_id,
                version_id,
                node_id,
                exc,
            )
            return None
