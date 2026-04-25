"""
ExtractionCandidateRepository — Phase 8 FG8.2.

extraction_candidates 테이블 CRUD.
모든 연산은 scope_profile_id ACL 슬롯을 지원하며, actor_type("user"|"agent")을 추적한다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from app.models.extraction import (
    ExtractionCandidate,
    ExtractionConfidenceScore,
    ExtractionMode,
    ExtractionStatus,
    HumanEditRecord,
)
from app.utils.time import utcnow
from app.utils.converters import uuid_str_or_none
from app.utils.json_utils import dumps_ko, loads_maybe

logger = logging.getLogger(__name__)


class ExtractionCandidateRepository:
    """추출 캔디데이트 저장소 (psycopg2 기반 raw SQL)."""

    def __init__(self, conn) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _json_dumps(self, obj: Any) -> str:
        return dumps_ko(obj, default=str)

    def _row_to_candidate(self, row: dict) -> ExtractionCandidate:
        confidence_raw = row.get("confidence_scores") or []
        confidence_raw = loads_maybe(confidence_raw)
        confidence_scores = [
            ExtractionConfidenceScore(**item) if isinstance(item, dict) else item
            for item in confidence_raw
        ]

        edits_raw = row.get("human_edits") or []
        edits_raw = loads_maybe(edits_raw)
        human_edits = [
            HumanEditRecord(**item) if isinstance(item, dict) else item
            for item in edits_raw
        ]

        extracted_fields = row.get("extracted_fields") or {}
        extracted_fields = loads_maybe(extracted_fields)

        return ExtractionCandidate(
            id=UUID(str(row["id"])),
            document_id=UUID(str(row["document_id"])),
            document_version=row["document_version"],
            extraction_schema_id=row["extraction_schema_id"],
            extraction_schema_version=row["extraction_schema_version"],
            extracted_fields=extracted_fields,
            confidence_scores=confidence_scores,
            extraction_model=row["extraction_model"],
            extraction_mode=ExtractionMode(row.get("extraction_mode", "deterministic")),
            extraction_latency_ms=row.get("extraction_latency_ms", 0),
            extraction_tokens=row.get("extraction_tokens"),
            extraction_cost_estimate=row.get("extraction_cost_estimate"),
            extraction_prompt_version=row.get("extraction_prompt_version"),
            document_content_hash=row.get("document_content_hash"),
            status=ExtractionStatus(row.get("status", "pending")),
            reviewed_by=row.get("reviewed_by"),
            reviewed_at=row.get("reviewed_at"),
            human_feedback=row.get("human_feedback"),
            human_edits=human_edits,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            actor_type=row.get("actor_type", "agent"),
            scope_profile_id=UUID(str(row["scope_profile_id"])) if row.get("scope_profile_id") else None,
            is_soft_deleted=row.get("is_soft_deleted", False),
        )

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        document_id: UUID,
        document_version: int,
        extraction_schema_id: str,
        extraction_schema_version: int,
        extracted_fields: Dict[str, Any],
        confidence_scores: List[ExtractionConfidenceScore],
        extraction_model: str,
        extraction_mode: ExtractionMode = ExtractionMode.DETERMINISTIC,
        extraction_latency_ms: int = 0,
        extraction_tokens: Optional[Dict[str, int]] = None,
        extraction_cost_estimate: Optional[float] = None,
        extraction_prompt_version: Optional[str] = None,
        document_content_hash: Optional[str] = None,
        scope_profile_id: Optional[UUID] = None,
        actor_type: str = "agent",
    ) -> ExtractionCandidate:
        """새 ExtractionCandidate 생성 (status=pending)."""
        now = utcnow()
        candidate_id = str(uuid4())

        scores_json = self._json_dumps([s.model_dump() for s in confidence_scores])
        fields_json = self._json_dumps(extracted_fields)
        tokens_json = self._json_dumps(extraction_tokens) if extraction_tokens else None

        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO extraction_candidates (
                    id, document_id, document_version,
                    extraction_schema_id, extraction_schema_version,
                    extracted_fields, confidence_scores,
                    extraction_model, extraction_mode, extraction_latency_ms,
                    extraction_tokens, extraction_cost_estimate,
                    extraction_prompt_version, document_content_hash,
                    status, actor_type, scope_profile_id,
                    created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb,
                    %s, %s, %s,
                    %s::jsonb, %s,
                    %s, %s,
                    'pending', %s, %s,
                    %s, %s
                )
                RETURNING
                    id, document_id, document_version,
                    extraction_schema_id, extraction_schema_version,
                    extracted_fields, confidence_scores,
                    extraction_model, extraction_mode, extraction_latency_ms,
                    extraction_tokens, extraction_cost_estimate,
                    extraction_prompt_version, document_content_hash,
                    status, reviewed_by, reviewed_at, human_feedback, human_edits,
                    created_at, updated_at, actor_type, scope_profile_id, is_soft_deleted
                """,
                (
                    candidate_id, str(document_id), document_version,
                    extraction_schema_id, extraction_schema_version,
                    fields_json, scores_json,
                    extraction_model, extraction_mode.value, extraction_latency_ms,
                    tokens_json, extraction_cost_estimate,
                    extraction_prompt_version, document_content_hash,
                    actor_type, uuid_str_or_none(scope_profile_id),
                    now, now,
                ),
            )
            row = cur.fetchone()

        return self._row_to_candidate(row)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_by_id(self, candidate_id: UUID) -> Optional[ExtractionCandidate]:
        """ID로 조회 (soft-deleted 제외)."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, document_id, document_version,
                       extraction_schema_id, extraction_schema_version,
                       extracted_fields, confidence_scores,
                       extraction_model, extraction_mode, extraction_latency_ms,
                       extraction_tokens, extraction_cost_estimate,
                       extraction_prompt_version, document_content_hash,
                       status, reviewed_by, reviewed_at, human_feedback, human_edits,
                       created_at, updated_at, actor_type, scope_profile_id, is_soft_deleted
                FROM extraction_candidates
                WHERE id = %s AND is_soft_deleted = FALSE
                """,
                (str(candidate_id),),
            )
            row = cur.fetchone()
        return self._row_to_candidate(row) if row else None

    def list_pending(
        self,
        *,
        scope_profile_id: Optional[UUID] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ExtractionCandidate]:
        """pending 상태 캔디데이트 목록 (최신순)."""
        params: list = []
        scope_filter = ""
        if scope_profile_id is not None:
            scope_filter = "AND scope_profile_id = %s"
            params.append(str(scope_profile_id))
        params += [limit, offset]

        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, document_id, document_version,
                       extraction_schema_id, extraction_schema_version,
                       extracted_fields, confidence_scores,
                       extraction_model, extraction_mode, extraction_latency_ms,
                       extraction_tokens, extraction_cost_estimate,
                       extraction_prompt_version, document_content_hash,
                       status, reviewed_by, reviewed_at, human_feedback, human_edits,
                       created_at, updated_at, actor_type, scope_profile_id, is_soft_deleted
                FROM extraction_candidates
                WHERE status = 'pending'
                  AND is_soft_deleted = FALSE
                  {scope_filter}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                params,
            )
            rows = cur.fetchall()
        return [self._row_to_candidate(r) for r in rows]

    def count_pending(self, *, scope_profile_id: Optional[UUID] = None) -> int:
        """pending 건수 조회."""
        params: list = []
        scope_filter = ""
        if scope_profile_id is not None:
            scope_filter = "AND scope_profile_id = %s"
            params.append(str(scope_profile_id))

        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*) FROM extraction_candidates
                WHERE status = 'pending' AND is_soft_deleted = FALSE
                {scope_filter}
                """,
                params,
            )
            row = cur.fetchone()
        return row["count"] if row else 0

    def list_by_status(
        self,
        status: ExtractionStatus,
        *,
        scope_profile_id: Optional[UUID] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ExtractionCandidate]:
        """상태별 목록 조회."""
        params: list = [status.value]
        scope_filter = ""
        if scope_profile_id is not None:
            scope_filter = "AND scope_profile_id = %s"
            params.append(str(scope_profile_id))
        params += [limit, offset]

        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, document_id, document_version,
                       extraction_schema_id, extraction_schema_version,
                       extracted_fields, confidence_scores,
                       extraction_model, extraction_mode, extraction_latency_ms,
                       extraction_tokens, extraction_cost_estimate,
                       extraction_prompt_version, document_content_hash,
                       status, reviewed_by, reviewed_at, human_feedback, human_edits,
                       created_at, updated_at, actor_type, scope_profile_id, is_soft_deleted
                FROM extraction_candidates
                WHERE status = %s AND is_soft_deleted = FALSE
                {scope_filter}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                params,
            )
            rows = cur.fetchall()
        return [self._row_to_candidate(r) for r in rows]

    def list_by_document(
        self,
        document_id: UUID,
        document_version: Optional[int] = None,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> List[ExtractionCandidate]:
        """문서별 추출 캔디데이트 목록."""
        params: list = [str(document_id)]
        version_filter = ""
        if document_version is not None:
            version_filter = "AND document_version = %s"
            params.append(document_version)
        params += [limit, offset]

        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, document_id, document_version,
                       extraction_schema_id, extraction_schema_version,
                       extracted_fields, confidence_scores,
                       extraction_model, extraction_mode, extraction_latency_ms,
                       extraction_tokens, extraction_cost_estimate,
                       extraction_prompt_version, document_content_hash,
                       status, reviewed_by, reviewed_at, human_feedback, human_edits,
                       created_at, updated_at, actor_type, scope_profile_id, is_soft_deleted
                FROM extraction_candidates
                WHERE document_id = %s AND is_soft_deleted = FALSE
                {version_filter}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                params,
            )
            rows = cur.fetchall()
        return [self._row_to_candidate(r) for r in rows]

    # ------------------------------------------------------------------
    # Admin queue 전용 조회 (documents join + 필터)
    # ------------------------------------------------------------------

    def list_for_admin_queue(
        self,
        *,
        statuses: Optional[List[ExtractionStatus]] = None,
        document_type: Optional[str] = None,
        scope_profile_id: Optional[UUID] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[List[dict], int]:
        """관리자 검토 큐 전용 목록 조회.

        `documents` 테이블과 LEFT JOIN 해 `document_title`/`document_summary`
        를 함께 가져온다. 문서가 삭제/미존재면 title 은 NULL — 라우터에서
        '(삭제된 문서)' 로 폴백한다.

        반환:
          (rows, total_count)
          - rows    : dict 리스트 (raw DB 컬럼 + document_title/summary 추가)
          - total_count: 동일 필터의 총 건수(페이지네이션 계산용)

        필터:
          - statuses        : 내부 상태값 리스트. None 이면 전체 상태.
          - document_type   : extraction_schema_id 필터 (대문자 기대).
          - scope_profile_id: S2 ⑥ 강제. None 이면 scope 필터 미적용
                              (관리자 모드에서 글로벌 뷰 허용).

        인덱스:
          - `idx_extraction_candidates_scope`
            (scope_profile_id, status, created_at DESC) 로
            scope + status 조합은 인덱스 타격.
          - document_type 필터는 `idx_extraction_candidates_schema_id` 사용.
        """
        where_parts = ["c.is_soft_deleted = FALSE"]
        params: list = []

        if statuses:
            where_parts.append("c.status = ANY(%s)")
            params.append([s.value for s in statuses])

        if document_type:
            where_parts.append("c.extraction_schema_id = %s")
            params.append(document_type)

        if scope_profile_id is not None:
            where_parts.append("c.scope_profile_id = %s")
            params.append(str(scope_profile_id))

        where_sql = " AND ".join(where_parts)

        # 두 쿼리를 한 트랜잭션에서 수행 — 일관된 total_count 보장.
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT c.id, c.document_id, c.document_version,
                       c.extraction_schema_id, c.extraction_schema_version,
                       c.extracted_fields, c.extraction_model,
                       c.status, c.reviewed_by, c.reviewed_at,
                       c.human_feedback, c.human_edits,
                       c.created_at, c.updated_at, c.actor_type,
                       c.scope_profile_id,
                       d.title      AS document_title,
                       d.summary    AS document_summary,
                       d.document_type AS document_document_type
                FROM extraction_candidates AS c
                LEFT JOIN documents AS d ON d.id = c.document_id
                WHERE {where_sql}
                ORDER BY c.created_at DESC
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            rows = cur.fetchall()

            cur.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM extraction_candidates AS c
                WHERE {where_sql}
                """,
                params,
            )
            count_row = cur.fetchone()
            total = count_row["total"] if count_row else 0

        return ([dict(r) for r in rows], int(total))

    def get_for_admin_detail(self, candidate_id: UUID) -> Optional[dict]:
        """관리자 상세 조회 — documents join 포함.

        soft-deleted 캔디데이트는 제외. 문서 자체가 삭제되어도 캔디데이트는
        유효하므로 LEFT JOIN 으로 title 이 NULL 일 수 있음.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.document_id, c.document_version,
                       c.extraction_schema_id, c.extraction_schema_version,
                       c.extracted_fields, c.extraction_model,
                       c.status, c.reviewed_by, c.reviewed_at,
                       c.human_feedback, c.human_edits,
                       c.created_at, c.updated_at, c.actor_type,
                       c.scope_profile_id,
                       d.title      AS document_title,
                       d.summary    AS document_summary,
                       d.document_type AS document_document_type
                FROM extraction_candidates AS c
                LEFT JOIN documents AS d ON d.id = c.document_id
                WHERE c.id = %s AND c.is_soft_deleted = FALSE
                """,
                (str(candidate_id),),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Update status
    # ------------------------------------------------------------------

    def update_status(
        self,
        candidate_id: UUID,
        *,
        new_status: ExtractionStatus,
        reviewed_by: Optional[str] = None,
        human_feedback: Optional[str] = None,
        human_edits: Optional[List[HumanEditRecord]] = None,
    ) -> Optional[ExtractionCandidate]:
        """상태 + 검토 정보 업데이트."""
        now = utcnow()
        edits_json = self._json_dumps(
            [e.model_dump(mode="json") for e in (human_edits or [])]
        )

        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE extraction_candidates
                SET status = %s,
                    reviewed_by = %s,
                    reviewed_at = %s,
                    human_feedback = %s,
                    human_edits = %s::jsonb,
                    updated_at = %s
                WHERE id = %s AND is_soft_deleted = FALSE
                RETURNING
                    id, document_id, document_version,
                    extraction_schema_id, extraction_schema_version,
                    extracted_fields, confidence_scores,
                    extraction_model, extraction_mode, extraction_latency_ms,
                    extraction_tokens, extraction_cost_estimate,
                    extraction_prompt_version, document_content_hash,
                    status, reviewed_by, reviewed_at, human_feedback, human_edits,
                    created_at, updated_at, actor_type, scope_profile_id, is_soft_deleted
                """,
                (
                    new_status.value, reviewed_by, now,
                    human_feedback, edits_json,
                    now, str(candidate_id),
                ),
            )
            row = cur.fetchone()
        return self._row_to_candidate(row) if row else None

    # ------------------------------------------------------------------
    # Soft delete
    # ------------------------------------------------------------------

    def soft_delete(self, candidate_id: UUID, deleted_by: str) -> bool:
        """소프트 삭제."""
        now = utcnow()
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE extraction_candidates
                SET is_soft_deleted = TRUE, deleted_at = %s, deleted_by = %s, updated_at = %s
                WHERE id = %s AND is_soft_deleted = FALSE
                """,
                (now, deleted_by, now, str(candidate_id)),
            )
            return cur.rowcount > 0
