"""
ExtractionRecordRepository + VerificationResultRepository — Phase 8 FG8.3 (task8-9).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

import psycopg2.extras

from app.models.extraction_record import (
    DiffDetail,
    ExtractionRecord,
    MatchStatus,
    VerificationResult,
)

logger = logging.getLogger(__name__)


def _row_to_record(row: dict) -> ExtractionRecord:
    result = row.get("extracted_result") or {}
    if isinstance(result, str):
        result = json.loads(result)

    return ExtractionRecord(
        id=UUID(str(row["id"])),
        extraction_candidate_id=UUID(str(row["extraction_candidate_id"])),
        document_id=UUID(str(row["document_id"])),
        document_version=row.get("document_version", 1),
        document_content_hash=row.get("document_content_hash"),
        extraction_schema_id=row["extraction_schema_id"],
        extraction_schema_version=row.get("extraction_schema_version", 1),
        extraction_model=row.get("extraction_model", "unknown"),
        model_version=row.get("model_version"),
        extraction_prompt_version=row.get("extraction_prompt_version"),
        extraction_mode=row.get("extraction_mode", "deterministic"),
        temperature=float(row.get("temperature", 0.0)),
        seed=row.get("seed"),
        extracted_result=result,
        extracted_timestamp=row.get("extracted_timestamp"),
        scope_profile_id=UUID(str(row["scope_profile_id"])) if row.get("scope_profile_id") else None,
        actor_type=row.get("actor_type", "agent"),
        created_at=row.get("created_at"),
    )


def _row_to_verification(row: dict) -> VerificationResult:
    diffs_raw = row.get("diff_details") or []
    if isinstance(diffs_raw, str):
        diffs_raw = json.loads(diffs_raw)
    diffs = [DiffDetail(**d) for d in diffs_raw] if diffs_raw else []

    return VerificationResult(
        id=UUID(str(row["id"])),
        extraction_candidate_id=UUID(str(row["extraction_candidate_id"])),
        verified_at=row["verified_at"],
        match_status=MatchStatus(row["match_status"]),
        field_match_count=row.get("field_match_count", 0),
        field_total_count=row.get("field_total_count", 0),
        field_accuracy=float(row.get("field_accuracy", 0.0)),
        diff_details=diffs,
        error_message=row.get("error_message"),
        verified_by=row.get("verified_by", "system"),
        actor_type=row.get("actor_type", "user"),
    )


class ExtractionRecordRepository:
    def __init__(self, conn):
        self._conn = conn

    def create(self, record: ExtractionRecord) -> ExtractionRecord:
        sql = """
            INSERT INTO extraction_records
                (extraction_candidate_id, document_id, document_version,
                 document_content_hash, extraction_schema_id, extraction_schema_version,
                 extraction_model, model_version, extraction_prompt_version,
                 extraction_mode, temperature, seed, extracted_result,
                 extracted_timestamp, scope_profile_id, actor_type)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING *
        """
        params = (
            str(record.extraction_candidate_id),
            str(record.document_id),
            record.document_version,
            record.document_content_hash,
            record.extraction_schema_id,
            record.extraction_schema_version,
            record.extraction_model,
            record.model_version,
            record.extraction_prompt_version,
            record.extraction_mode,
            record.temperature,
            record.seed,
            json.dumps(record.extracted_result),
            record.extracted_timestamp,
            str(record.scope_profile_id) if record.scope_profile_id else None,
            record.actor_type,
        )
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return _row_to_record(dict(row))

    def get_by_candidate(self, extraction_candidate_id: UUID) -> Optional[ExtractionRecord]:
        sql = """
            SELECT * FROM extraction_records
            WHERE extraction_candidate_id = %s
            ORDER BY created_at DESC LIMIT 1
        """
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (str(extraction_candidate_id),))
            row = cur.fetchone()
        return _row_to_record(dict(row)) if row else None

    def get_by_id(self, record_id: UUID) -> Optional[ExtractionRecord]:
        sql = "SELECT * FROM extraction_records WHERE id = %s"
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (str(record_id),))
            row = cur.fetchone()
        return _row_to_record(dict(row)) if row else None


class VerificationResultRepository:
    def __init__(self, conn):
        self._conn = conn

    def create(self, vr: VerificationResult) -> VerificationResult:
        sql = """
            INSERT INTO verification_results
                (extraction_candidate_id, verified_at, match_status,
                 field_match_count, field_total_count, field_accuracy,
                 diff_details, error_message, verified_by, actor_type)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING *
        """
        params = (
            str(vr.extraction_candidate_id),
            vr.verified_at,
            vr.match_status.value,
            vr.field_match_count,
            vr.field_total_count,
            vr.field_accuracy,
            json.dumps([d.model_dump() for d in vr.diff_details]),
            vr.error_message,
            vr.verified_by,
            vr.actor_type,
        )
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return _row_to_verification(dict(row))

    def list_by_candidate(
        self,
        extraction_candidate_id: UUID,
        limit: int = 20,
    ) -> List[VerificationResult]:
        sql = """
            SELECT * FROM verification_results
            WHERE extraction_candidate_id = %s
            ORDER BY verified_at DESC LIMIT %s
        """
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (str(extraction_candidate_id), limit))
            rows = cur.fetchall()
        return [_row_to_verification(dict(r)) for r in rows]
