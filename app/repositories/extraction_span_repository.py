"""
ExtractionSpanRepository — Phase 8 FG8.3 (task8-8).

raw psycopg2 기반 extraction_spans CRUD.
"""
from __future__ import annotations

import json
import logging
from typing import List, Optional
from uuid import UUID

import psycopg2.extras

from app.models.extraction_span import SourceSpan

logger = logging.getLogger(__name__)


def _row_to_span(row: dict) -> SourceSpan:
    offset_raw = row.get("span_offset")
    if isinstance(offset_raw, (list, tuple)):
        offset = tuple(offset_raw)
    elif isinstance(offset_raw, str):
        parsed = json.loads(offset_raw)
        offset = tuple(parsed)
    else:
        offset = (0, 1)

    return SourceSpan(
        id=UUID(str(row["id"])),
        document_id=UUID(str(row["document_id"])),
        version_id=UUID(str(row["version_id"])) if row.get("version_id") else None,
        node_id=UUID(str(row["node_id"])) if row.get("node_id") else None,
        span_offset=offset,
        source_text=row["source_text"],
        content_hash=row.get("content_hash"),
        created_at=row.get("created_at"),
    )


class ExtractionSpanRepository:
    def __init__(self, conn):
        self._conn = conn

    def create(
        self,
        extraction_candidate_id: UUID,
        field_name: str,
        span: SourceSpan,
    ) -> SourceSpan:
        sql = """
            INSERT INTO extraction_spans
                (extraction_candidate_id, field_name, document_id, version_id, node_id,
                 span_start, span_end, source_text, content_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, document_id, version_id, node_id,
                      span_start, span_end, source_text, content_hash, created_at
        """
        params = (
            str(extraction_candidate_id),
            field_name,
            str(span.document_id),
            str(span.version_id) if span.version_id else None,
            str(span.node_id) if span.node_id else None,
            span.span_offset[0],
            span.span_offset[1],
            span.source_text,
            span.content_hash,
        )
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()

        row_dict = dict(row)
        row_dict["span_offset"] = (row_dict.pop("span_start"), row_dict.pop("span_end"))
        return _row_to_span(row_dict)

    def list_by_candidate(
        self,
        extraction_candidate_id: UUID,
    ) -> List[dict]:
        """(field_name, SourceSpan) 쌍 목록을 반환한다."""
        sql = """
            SELECT id, field_name, document_id, version_id, node_id,
                   span_start, span_end, source_text, content_hash, created_at
            FROM extraction_spans
            WHERE extraction_candidate_id = %s
            ORDER BY field_name, span_start
        """
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (str(extraction_candidate_id),))
            rows = cur.fetchall()

        result = []
        for row in rows:
            row_dict = dict(row)
            row_dict["span_offset"] = (row_dict.pop("span_start"), row_dict.pop("span_end"))
            result.append({
                "field_name": row_dict.pop("field_name"),
                "span": _row_to_span(row_dict),
            })
        return result

    def delete_by_candidate(self, extraction_candidate_id: UUID) -> int:
        sql = "DELETE FROM extraction_spans WHERE extraction_candidate_id = %s"
        with self._conn.cursor() as cur:
            cur.execute(sql, (str(extraction_candidate_id),))
            return cur.rowcount
