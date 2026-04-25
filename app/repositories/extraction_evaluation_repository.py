"""
ExtractionEvaluationRepository + GoldenExtractionSetRepository — Phase 8 FG8.3 (task8-10).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import List, Optional
from uuid import UUID

import psycopg2.extras

from app.models.extraction_evaluation import (
    ExtractionEvaluationResult,
    ExtractionMetrics,
    FieldEvaluationDetail,
    GoldenExtractionItem,
    GoldenExtractionSet,
)
from app.utils.time import utcnow
from app.utils.converters import uuid_str_or_none
from app.utils.json_utils import loads_maybe

logger = logging.getLogger(__name__)


def _row_to_evaluation(row: dict) -> ExtractionEvaluationResult:
    metrics_raw = row.get("metrics") or {}
    metrics_raw = loads_maybe(metrics_raw)
    metrics = ExtractionMetrics(**metrics_raw)

    details_raw = row.get("field_details") or []
    details_raw = loads_maybe(details_raw)
    details = [FieldEvaluationDetail(**d) for d in details_raw]

    return ExtractionEvaluationResult(
        id=UUID(str(row["id"])),
        golden_set_id=UUID(str(row["golden_set_id"])) if row.get("golden_set_id") else None,
        golden_item_id=UUID(str(row["golden_item_id"])) if row.get("golden_item_id") else None,
        extraction_candidate_id=UUID(str(row["extraction_candidate_id"]))
        if row.get("extraction_candidate_id") else None,
        metrics=metrics,
        field_details=details,
        evaluated_at=row.get("evaluated_at"),
        evaluated_by=row.get("evaluated_by", "system"),
        actor_type=row.get("actor_type", "user"),
        scope_profile_id=UUID(str(row["scope_profile_id"]))
        if row.get("scope_profile_id") else None,
    )


class ExtractionEvaluationRepository:
    def __init__(self, conn):
        self._conn = conn

    def create(self, result: ExtractionEvaluationResult) -> ExtractionEvaluationResult:
        sql = """
            INSERT INTO extraction_evaluations
                (golden_set_id, golden_item_id, extraction_candidate_id,
                 metrics, field_details, evaluated_at, evaluated_by,
                 actor_type, scope_profile_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING *
        """
        params = (
            uuid_str_or_none(result.golden_set_id),
            uuid_str_or_none(result.golden_item_id),
            uuid_str_or_none(result.extraction_candidate_id),
            json.dumps(result.metrics.model_dump()),
            json.dumps([d.model_dump() for d in result.field_details]),
            result.evaluated_at or utcnow(),
            result.evaluated_by,
            result.actor_type,
            uuid_str_or_none(result.scope_profile_id),
        )
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return _row_to_evaluation(dict(row))

    def get_by_id(self, eval_id: UUID) -> Optional[ExtractionEvaluationResult]:
        sql = "SELECT * FROM extraction_evaluations WHERE id = %s"
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (str(eval_id),))
            row = cur.fetchone()
        return _row_to_evaluation(dict(row)) if row else None

    def list_by_golden_set(
        self,
        golden_set_id: UUID,
        limit: int = 20,
        offset: int = 0,
    ) -> List[ExtractionEvaluationResult]:
        sql = """
            SELECT * FROM extraction_evaluations
            WHERE golden_set_id = %s
            ORDER BY evaluated_at DESC LIMIT %s OFFSET %s
        """
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (str(golden_set_id), limit, offset))
            rows = cur.fetchall()
        return [_row_to_evaluation(dict(r)) for r in rows]


class GoldenExtractionSetRepository:
    def __init__(self, conn):
        self._conn = conn

    def create(self, gset: GoldenExtractionSet) -> GoldenExtractionSet:
        sql = """
            INSERT INTO golden_extraction_sets
                (name, description, document_type, version, created_by,
                 scope_profile_id, actor_type)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            RETURNING id, name, description, document_type, version,
                      created_by, scope_profile_id, actor_type, created_at, updated_at
        """
        params = (
            gset.name, gset.description, gset.document_type, gset.version,
            gset.created_by,
            uuid_str_or_none(gset.scope_profile_id),
            gset.actor_type,
        )
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = dict(cur.fetchone())

        return GoldenExtractionSet(
            id=UUID(str(row["id"])),
            name=row["name"],
            description=row.get("description"),
            document_type=row["document_type"],
            version=row.get("version", 1),
            created_by=row["created_by"],
            scope_profile_id=UUID(str(row["scope_profile_id"])) if row.get("scope_profile_id") else None,
            actor_type=row.get("actor_type", "user"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    def get_by_id(self, set_id: UUID) -> Optional[GoldenExtractionSet]:
        sql = """
            SELECT id, name, description, document_type, version,
                   created_by, scope_profile_id, actor_type, created_at, updated_at
            FROM golden_extraction_sets WHERE id = %s
        """
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (str(set_id),))
            row = cur.fetchone()
        if not row:
            return None

        row = dict(row)
        return GoldenExtractionSet(
            id=UUID(str(row["id"])),
            name=row["name"],
            description=row.get("description"),
            document_type=row["document_type"],
            version=row.get("version", 1),
            created_by=row["created_by"],
            scope_profile_id=UUID(str(row["scope_profile_id"])) if row.get("scope_profile_id") else None,
            actor_type=row.get("actor_type", "user"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )


class GoldenExtractionItemRepository:
    def __init__(self, conn):
        self._conn = conn

    def create(self, item: GoldenExtractionItem) -> GoldenExtractionItem:
        sql = """
            INSERT INTO golden_extraction_items
                (golden_set_id, document_id, document_version, document_type,
                 expected_fields, expected_spans, created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            RETURNING id, golden_set_id, document_id, document_version, document_type,
                      expected_fields, expected_spans, created_by, created_at
        """
        params = (
            uuid_str_or_none(item.golden_set_id),
            str(item.document_id),
            item.document_version,
            item.document_type,
            json.dumps([f.model_dump() for f in item.expected_fields]),
            json.dumps([s.model_dump() for s in item.expected_spans]),
            item.created_by,
        )
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = dict(cur.fetchone())

        return self._row_to_item(row)

    def list_by_set(self, golden_set_id: UUID) -> List[GoldenExtractionItem]:
        sql = "SELECT * FROM golden_extraction_items WHERE golden_set_id = %s ORDER BY created_at"
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (str(golden_set_id),))
            rows = cur.fetchall()
        return [self._row_to_item(dict(r)) for r in rows]

    def _row_to_item(self, row: dict) -> GoldenExtractionItem:
        from app.models.extraction_evaluation import ExpectedField, ExpectedSpan

        fields_raw = row.get("expected_fields") or []
        fields_raw = loads_maybe(fields_raw)
        expected_fields = [ExpectedField(**f) for f in fields_raw]

        spans_raw = row.get("expected_spans") or []
        spans_raw = loads_maybe(spans_raw)
        expected_spans = [ExpectedSpan(**s) for s in spans_raw]

        return GoldenExtractionItem(
            id=UUID(str(row["id"])),
            golden_set_id=UUID(str(row["golden_set_id"])) if row.get("golden_set_id") else None,
            document_id=UUID(str(row["document_id"])),
            document_version=row.get("document_version", 1),
            document_type=row["document_type"],
            expected_fields=expected_fields,
            expected_spans=expected_spans,
            created_by=row.get("created_by", "system"),
            created_at=row.get("created_at"),
        )
