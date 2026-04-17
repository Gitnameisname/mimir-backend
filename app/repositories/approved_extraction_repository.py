"""
ApprovedExtractionRepository — Phase 8 FG8.2.

approved_extractions 테이블 CRUD.
Scope Profile ACL 슬롯 지원 (S2 원칙 ⑥).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from app.models.approved_extraction import ApprovedExtraction, HumanEdit

logger = logging.getLogger(__name__)


class ApprovedExtractionRepository:
    """승인된 추출 결과 저장소 (psycopg2 기반 raw SQL)."""

    def __init__(self, conn) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _json_dumps(self, obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False, default=str)

    def _row_to_model(self, row: dict) -> ApprovedExtraction:
        edits_raw = row.get("human_edits") or []
        if isinstance(edits_raw, str):
            edits_raw = json.loads(edits_raw)
        human_edits = [HumanEdit(**item) if isinstance(item, dict) else item for item in edits_raw]

        approved_fields = row.get("approved_fields") or {}
        if isinstance(approved_fields, str):
            approved_fields = json.loads(approved_fields)

        tokens = row.get("extraction_tokens")
        if isinstance(tokens, str):
            tokens = json.loads(tokens)

        return ApprovedExtraction(
            id=UUID(str(row["id"])),
            candidate_id=UUID(str(row["candidate_id"])) if row.get("candidate_id") else None,
            document_id=UUID(str(row["document_id"])),
            document_version=row["document_version"],
            extraction_schema_id=row["extraction_schema_id"],
            extraction_schema_version=row["extraction_schema_version"],
            extraction_model=row["extraction_model"],
            extraction_latency_ms=row.get("extraction_latency_ms", 0),
            extraction_tokens=tokens,
            extraction_cost_estimate=row.get("extraction_cost_estimate"),
            extraction_prompt_version=row.get("extraction_prompt_version"),
            approved_fields=approved_fields,
            human_edits=human_edits,
            approved_by=row["approved_by"],
            approved_at=row["approved_at"],
            approval_comment=row.get("approval_comment"),
            actor_type=row.get("actor_type", "user"),
            scope_profile_id=UUID(str(row["scope_profile_id"])) if row.get("scope_profile_id") else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            is_soft_deleted=row.get("is_soft_deleted", False),
        )

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        candidate_id: Optional[UUID],
        document_id: UUID,
        document_version: int,
        extraction_schema_id: str,
        extraction_schema_version: int,
        extraction_model: str,
        extraction_latency_ms: int,
        extraction_tokens: Optional[Dict[str, int]],
        extraction_cost_estimate: Optional[float],
        extraction_prompt_version: Optional[str],
        approved_fields: Dict[str, Any],
        human_edits: List[HumanEdit],
        approved_by: str,
        approved_at: datetime,
        approval_comment: Optional[str],
        actor_type: str = "user",
        scope_profile_id: Optional[UUID] = None,
    ) -> ApprovedExtraction:
        """새 ApprovedExtraction 저장."""
        now = datetime.now(timezone.utc)
        ae_id = str(uuid4())

        edits_json = self._json_dumps([e.model_dump(mode="json") for e in human_edits])
        fields_json = self._json_dumps(approved_fields)
        tokens_json = self._json_dumps(extraction_tokens) if extraction_tokens else None

        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO approved_extractions (
                    id, candidate_id, document_id, document_version,
                    extraction_schema_id, extraction_schema_version,
                    extraction_model, extraction_latency_ms,
                    extraction_tokens, extraction_cost_estimate, extraction_prompt_version,
                    approved_fields, human_edits,
                    approved_by, approved_at, approval_comment,
                    actor_type, scope_profile_id,
                    created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s::jsonb, %s, %s,
                    %s::jsonb, %s::jsonb,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s
                )
                RETURNING
                    id, candidate_id, document_id, document_version,
                    extraction_schema_id, extraction_schema_version,
                    extraction_model, extraction_latency_ms,
                    extraction_tokens, extraction_cost_estimate, extraction_prompt_version,
                    approved_fields, human_edits,
                    approved_by, approved_at, approval_comment,
                    actor_type, scope_profile_id,
                    created_at, updated_at, is_soft_deleted
                """,
                (
                    ae_id, str(candidate_id) if candidate_id else None,
                    str(document_id), document_version,
                    extraction_schema_id, extraction_schema_version,
                    extraction_model, extraction_latency_ms,
                    tokens_json, extraction_cost_estimate, extraction_prompt_version,
                    fields_json, edits_json,
                    approved_by, approved_at, approval_comment,
                    actor_type, str(scope_profile_id) if scope_profile_id else None,
                    now, now,
                ),
            )
            row = cur.fetchone()

        return self._row_to_model(row)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_by_id(self, ae_id: UUID) -> Optional[ApprovedExtraction]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, candidate_id, document_id, document_version,
                       extraction_schema_id, extraction_schema_version,
                       extraction_model, extraction_latency_ms,
                       extraction_tokens, extraction_cost_estimate, extraction_prompt_version,
                       approved_fields, human_edits,
                       approved_by, approved_at, approval_comment,
                       actor_type, scope_profile_id,
                       created_at, updated_at, is_soft_deleted
                FROM approved_extractions
                WHERE id = %s AND is_soft_deleted = FALSE
                """,
                (str(ae_id),),
            )
            row = cur.fetchone()
        return self._row_to_model(row) if row else None

    def get_by_candidate(self, candidate_id: UUID) -> Optional[ApprovedExtraction]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, candidate_id, document_id, document_version,
                       extraction_schema_id, extraction_schema_version,
                       extraction_model, extraction_latency_ms,
                       extraction_tokens, extraction_cost_estimate, extraction_prompt_version,
                       approved_fields, human_edits,
                       approved_by, approved_at, approval_comment,
                       actor_type, scope_profile_id,
                       created_at, updated_at, is_soft_deleted
                FROM approved_extractions
                WHERE candidate_id = %s AND is_soft_deleted = FALSE
                ORDER BY created_at DESC LIMIT 1
                """,
                (str(candidate_id),),
            )
            row = cur.fetchone()
        return self._row_to_model(row) if row else None

    def list_by_document(
        self,
        document_id: UUID,
        *,
        scope_profile_id: Optional[UUID] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[ApprovedExtraction]:
        params: list = [str(document_id)]
        scope_filter = ""
        if scope_profile_id is not None:
            scope_filter = "AND scope_profile_id = %s"
            params.append(str(scope_profile_id))
        params += [limit, offset]

        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, candidate_id, document_id, document_version,
                       extraction_schema_id, extraction_schema_version,
                       extraction_model, extraction_latency_ms,
                       extraction_tokens, extraction_cost_estimate, extraction_prompt_version,
                       approved_fields, human_edits,
                       approved_by, approved_at, approval_comment,
                       actor_type, scope_profile_id,
                       created_at, updated_at, is_soft_deleted
                FROM approved_extractions
                WHERE document_id = %s AND is_soft_deleted = FALSE
                {scope_filter}
                ORDER BY approved_at DESC
                LIMIT %s OFFSET %s
                """,
                params,
            )
            rows = cur.fetchall()
        return [self._row_to_model(r) for r in rows]

    def list_recent(
        self,
        *,
        scope_profile_id: Optional[UUID] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ApprovedExtraction]:
        params: list = []
        scope_filter = ""
        if scope_profile_id is not None:
            scope_filter = "WHERE scope_profile_id = %s AND is_soft_deleted = FALSE"
            params.append(str(scope_profile_id))
        else:
            scope_filter = "WHERE is_soft_deleted = FALSE"
        params += [limit, offset]

        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, candidate_id, document_id, document_version,
                       extraction_schema_id, extraction_schema_version,
                       extraction_model, extraction_latency_ms,
                       extraction_tokens, extraction_cost_estimate, extraction_prompt_version,
                       approved_fields, human_edits,
                       approved_by, approved_at, approval_comment,
                       actor_type, scope_profile_id,
                       created_at, updated_at, is_soft_deleted
                FROM approved_extractions
                {scope_filter}
                ORDER BY approved_at DESC
                LIMIT %s OFFSET %s
                """,
                params,
            )
            rows = cur.fetchall()
        return [self._row_to_model(r) for r in rows]

    # ------------------------------------------------------------------
    # Soft delete
    # ------------------------------------------------------------------

    def soft_delete(self, ae_id: UUID, deleted_by: str) -> bool:
        now = datetime.now(timezone.utc)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE approved_extractions
                SET is_soft_deleted = TRUE, deleted_at = %s, deleted_by = %s, updated_at = %s
                WHERE id = %s AND is_soft_deleted = FALSE
                """,
                (now, deleted_by, now, str(ae_id)),
            )
            return cur.rowcount > 0
