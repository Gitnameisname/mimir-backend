"""FG 0-3 커버리지 보강 — extraction_span_repository 유닛 테스트 (세션 13-E).

대상: `backend/app/repositories/extraction_span_repository.py` (109줄)

커버 범위:
  - _row_to_span (offset 타입 분기: list/tuple / str JSON / 기본값 (0,1))
  - ExtractionSpanRepository.create (기본 / version_id None / node_id None)
  - list_by_candidate (빈 결과 / rows 반환 + (field_name, span) 쌍 변환)
  - delete_by_candidate (rowcount 반환)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from app.repositories.extraction_span_repository import (
    ExtractionSpanRepository,
    _row_to_span,
)
from app.models.extraction_span import SourceSpan


def _mk_cur(fetchone_values=None, fetchall_values=None, rowcount=0):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.execute = MagicMock()
    if fetchone_values is not None:
        cur.fetchone = MagicMock(side_effect=list(fetchone_values))
    else:
        cur.fetchone = MagicMock(return_value=None)
    if fetchall_values is not None:
        cur.fetchall = MagicMock(side_effect=list(fetchall_values))
    else:
        cur.fetchall = MagicMock(return_value=[])
    cur.rowcount = rowcount
    return cur


def _mk_conn(cur):
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn


def _mk_span_row(offset=(10, 20), version_id="22222222-2222-2222-2222-222222222222"):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "document_id": "33333333-3333-3333-3333-333333333333",
        "version_id": version_id,
        "node_id": None,
        "span_offset": offset,
        "source_text": "본문 일부",
        "content_hash": "abc",
        "created_at": now,
    }


# ---------------------------------------------------------------------------
# 1. _row_to_span (offset 분기)
# ---------------------------------------------------------------------------


def test_row_to_span_offset_as_tuple():
    span = _row_to_span(_mk_span_row(offset=(5, 15)))
    assert span.span_offset == (5, 15)


def test_row_to_span_offset_as_list_converts_to_tuple():
    span = _row_to_span(_mk_span_row(offset=[3, 9]))
    assert span.span_offset == (3, 9)


def test_row_to_span_offset_as_json_string():
    span = _row_to_span(_mk_span_row(offset=json.dumps([0, 100])))
    assert span.span_offset == (0, 100)


def test_row_to_span_offset_missing_uses_default():
    row = _mk_span_row()
    row["span_offset"] = None
    span = _row_to_span(row)
    assert span.span_offset == (0, 1)


def test_row_to_span_null_version_and_node():
    row = _mk_span_row(version_id=None)
    row["node_id"] = None
    span = _row_to_span(row)
    assert span.version_id is None
    assert span.node_id is None


# ---------------------------------------------------------------------------
# 2. ExtractionSpanRepository.create
# ---------------------------------------------------------------------------


def test_create_span_basic():
    # INSERT RETURNING 결과는 span_start/span_end 개별 컬럼으로 반환
    now = datetime.now(timezone.utc)
    returning_row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "document_id": "33333333-3333-3333-3333-333333333333",
        "version_id": "22222222-2222-2222-2222-222222222222",
        "node_id": None,
        "span_start": 10,
        "span_end": 20,
        "source_text": "본문",
        "content_hash": "abc",
        "created_at": now,
    }
    cur = _mk_cur(fetchone_values=[returning_row])
    repo = ExtractionSpanRepository(_mk_conn(cur))
    span_in = SourceSpan(
        document_id=UUID("33333333-3333-3333-3333-333333333333"),
        version_id=UUID("22222222-2222-2222-2222-222222222222"),
        span_offset=(10, 20),
        source_text="본문",
        content_hash="abc",
    )
    result = repo.create(
        extraction_candidate_id=UUID("44444444-4444-4444-4444-444444444444"),
        field_name="title",
        span=span_in,
    )
    assert result.span_offset == (10, 20)
    sql = cur.execute.call_args[0][0]
    assert "INSERT INTO extraction_spans" in sql


def test_create_span_with_null_version_and_node():
    now = datetime.now(timezone.utc)
    returning_row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "document_id": "33333333-3333-3333-3333-333333333333",
        "version_id": None,
        "node_id": None,
        "span_start": 0,
        "span_end": 5,
        "source_text": "text",
        "content_hash": None,
        "created_at": now,
    }
    cur = _mk_cur(fetchone_values=[returning_row])
    repo = ExtractionSpanRepository(_mk_conn(cur))
    span_in = SourceSpan(
        document_id=UUID("33333333-3333-3333-3333-333333333333"),
        version_id=None,
        node_id=None,
        span_offset=(0, 5),
        source_text="text",
        content_hash=None,
    )
    result = repo.create(
        extraction_candidate_id=UUID("44444444-4444-4444-4444-444444444444"),
        field_name="body",
        span=span_in,
    )
    assert result.version_id is None
    assert result.node_id is None
    params = cur.execute.call_args[0][1]
    # version_id / node_id 위치에 None 이 있어야 함
    assert None in params


# ---------------------------------------------------------------------------
# 3. list_by_candidate
# ---------------------------------------------------------------------------


def test_list_by_candidate_empty():
    cur = _mk_cur(fetchall_values=[[]])
    repo = ExtractionSpanRepository(_mk_conn(cur))
    assert repo.list_by_candidate(
        UUID("44444444-4444-4444-4444-444444444444")
    ) == []


def test_list_by_candidate_converts_span_start_end_to_offset():
    now = datetime.now(timezone.utc)
    rows = [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "field_name": "title",
            "document_id": "33333333-3333-3333-3333-333333333333",
            "version_id": "22222222-2222-2222-2222-222222222222",
            "node_id": None,
            "span_start": 0,
            "span_end": 10,
            "source_text": "T",
            "content_hash": "h",
            "created_at": now,
        },
        {
            "id": "55555555-5555-5555-5555-555555555555",
            "field_name": "body",
            "document_id": "33333333-3333-3333-3333-333333333333",
            "version_id": None,
            "node_id": None,
            "span_start": 20,
            "span_end": 40,
            "source_text": "B",
            "content_hash": None,
            "created_at": now,
        },
    ]
    cur = _mk_cur(fetchall_values=[rows])
    repo = ExtractionSpanRepository(_mk_conn(cur))
    result = repo.list_by_candidate(
        UUID("44444444-4444-4444-4444-444444444444")
    )
    assert len(result) == 2
    assert result[0]["field_name"] == "title"
    assert result[0]["span"].span_offset == (0, 10)
    assert result[1]["field_name"] == "body"
    assert result[1]["span"].span_offset == (20, 40)


# ---------------------------------------------------------------------------
# 4. delete_by_candidate
# ---------------------------------------------------------------------------


def test_delete_by_candidate_returns_rowcount():
    cur = _mk_cur(rowcount=3)
    repo = ExtractionSpanRepository(_mk_conn(cur))
    assert repo.delete_by_candidate(
        UUID("44444444-4444-4444-4444-444444444444")
    ) == 3


def test_delete_by_candidate_returns_zero_when_no_rows():
    cur = _mk_cur(rowcount=0)
    repo = ExtractionSpanRepository(_mk_conn(cur))
    assert repo.delete_by_candidate(
        UUID("44444444-4444-4444-4444-444444444444")
    ) == 0
