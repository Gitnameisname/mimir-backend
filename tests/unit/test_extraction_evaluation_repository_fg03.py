"""FG 0-3 커버리지 보강 — extraction_evaluation_repository 유닛 테스트 (세션 13-C).

대상: `backend/app/repositories/extraction_evaluation_repository.py` (226줄)

커버 범위:
  - _row_to_evaluation (JSON str/dict 파싱, scope 유무, defaults)
  - ExtractionEvaluationRepository.create / get_by_id / list_by_golden_set
  - GoldenExtractionSetRepository.create / get_by_id (found/None)
  - GoldenExtractionItemRepository.create / list_by_set / _row_to_item (JSON str + dict)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from app.repositories.extraction_evaluation_repository import (
    ExtractionEvaluationRepository,
    GoldenExtractionItemRepository,
    GoldenExtractionSetRepository,
    _row_to_evaluation,
)
from app.models.extraction_evaluation import (
    ExpectedField,
    ExpectedSpan,
    ExtractionEvaluationResult,
    ExtractionMetrics,
    FieldEvaluationDetail,
    GoldenExtractionItem,
    GoldenExtractionSet,
)


def _mk_cur(fetchone_values=None, fetchall_values=None):
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
    return cur


def _mk_conn(cur):
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn


def _mk_metrics_dict():
    return {
        "field_accuracy": 0.9,
        "span_accuracy": 0.85,
        "required_field_coverage": 0.95,
        "type_correctness": 0.92,
        "overall_score": 0.88,
    }


def _mk_eval_row(scope_profile_id=None, metrics_as_str=False, details_as_str=False):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    metrics = _mk_metrics_dict()
    details = []
    if metrics_as_str:
        metrics = json.dumps(metrics)
    if details_as_str:
        details = json.dumps(details)
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "golden_set_id": "22222222-2222-2222-2222-222222222222",
        "golden_item_id": "33333333-3333-3333-3333-333333333333",
        "extraction_candidate_id": "44444444-4444-4444-4444-444444444444",
        "metrics": metrics,
        "field_details": details,
        "evaluated_at": now,
        "evaluated_by": "system",
        "actor_type": "user",
        "scope_profile_id": scope_profile_id,
    }


# ---------------------------------------------------------------------------
# 1. _row_to_evaluation
# ---------------------------------------------------------------------------


def test_row_to_evaluation_dict_inputs():
    result = _row_to_evaluation(_mk_eval_row())
    assert isinstance(result.metrics, ExtractionMetrics)
    assert result.metrics.field_accuracy == 0.9


def test_row_to_evaluation_json_string_inputs():
    result = _row_to_evaluation(
        _mk_eval_row(metrics_as_str=True, details_as_str=True)
    )
    assert result.metrics.field_accuracy == 0.9


def test_row_to_evaluation_with_scope():
    sid = str(uuid4())
    result = _row_to_evaluation(_mk_eval_row(scope_profile_id=sid))
    assert result.scope_profile_id is not None


def test_row_to_evaluation_null_ids():
    row = _mk_eval_row()
    row["golden_set_id"] = None
    row["golden_item_id"] = None
    row["extraction_candidate_id"] = None
    result = _row_to_evaluation(row)
    assert result.golden_set_id is None
    assert result.golden_item_id is None
    assert result.extraction_candidate_id is None


def test_row_to_evaluation_defaults_missing_evaluated_by():
    row = _mk_eval_row()
    del row["evaluated_by"]
    result = _row_to_evaluation(row)
    assert result.evaluated_by == "system"


# ---------------------------------------------------------------------------
# 2. ExtractionEvaluationRepository
# ---------------------------------------------------------------------------


def test_eval_create_executes_insert():
    cur = _mk_cur(fetchone_values=[_mk_eval_row()])
    repo = ExtractionEvaluationRepository(_mk_conn(cur))
    result = ExtractionEvaluationResult(
        metrics=ExtractionMetrics(**_mk_metrics_dict()),
        field_details=[],
        evaluated_by="system",
    )
    saved = repo.create(result)
    assert saved.metrics.field_accuracy == 0.9
    sql = cur.execute.call_args[0][0]
    assert "INSERT INTO extraction_evaluations" in sql


def test_eval_get_by_id_found():
    cur = _mk_cur(fetchone_values=[_mk_eval_row()])
    repo = ExtractionEvaluationRepository(_mk_conn(cur))
    result = repo.get_by_id(UUID("11111111-1111-1111-1111-111111111111"))
    assert result is not None


def test_eval_get_by_id_not_found():
    cur = _mk_cur(fetchone_values=[None])
    repo = ExtractionEvaluationRepository(_mk_conn(cur))
    assert repo.get_by_id(UUID("11111111-1111-1111-1111-111111111111")) is None


def test_eval_list_by_golden_set():
    cur = _mk_cur(fetchall_values=[[_mk_eval_row(), _mk_eval_row()]])
    repo = ExtractionEvaluationRepository(_mk_conn(cur))
    result = repo.list_by_golden_set(
        UUID("22222222-2222-2222-2222-222222222222")
    )
    assert len(result) == 2


# ---------------------------------------------------------------------------
# 3. GoldenExtractionSetRepository
# ---------------------------------------------------------------------------


def _mk_set_row():
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "REPORT 골든셋 v1",
        "description": "테스트용",
        "document_type": "REPORT",
        "version": 1,
        "created_by": "user-1",
        "scope_profile_id": None,
        "actor_type": "user",
        "created_at": now,
        "updated_at": now,
    }


def test_golden_set_create():
    cur = _mk_cur(fetchone_values=[_mk_set_row()])
    repo = GoldenExtractionSetRepository(_mk_conn(cur))
    gset = GoldenExtractionSet(
        name="REPORT 골든셋 v1",
        description="테스트용",
        document_type="REPORT",
        created_by="user-1",
    )
    saved = repo.create(gset)
    assert saved.name == "REPORT 골든셋 v1"


def test_golden_set_get_by_id_found():
    cur = _mk_cur(fetchone_values=[_mk_set_row()])
    repo = GoldenExtractionSetRepository(_mk_conn(cur))
    result = repo.get_by_id(UUID("11111111-1111-1111-1111-111111111111"))
    assert result is not None
    assert result.name == "REPORT 골든셋 v1"


def test_golden_set_get_by_id_not_found():
    cur = _mk_cur(fetchone_values=[None])
    repo = GoldenExtractionSetRepository(_mk_conn(cur))
    assert repo.get_by_id(UUID("11111111-1111-1111-1111-111111111111")) is None


# ---------------------------------------------------------------------------
# 4. GoldenExtractionItemRepository
# ---------------------------------------------------------------------------


def _mk_item_row(fields_as_str=False, spans_as_str=False):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    fields = [{"field_name": "title", "expected_value": "T"}]
    spans = []
    if fields_as_str:
        fields = json.dumps(fields)
    if spans_as_str:
        spans = json.dumps(spans)
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "golden_set_id": "22222222-2222-2222-2222-222222222222",
        "document_id": "33333333-3333-3333-3333-333333333333",
        "document_version": 1,
        "document_type": "REPORT",
        "expected_fields": fields,
        "expected_spans": spans,
        "created_by": "system",
        "created_at": now,
    }


def test_golden_item_create():
    cur = _mk_cur(fetchone_values=[_mk_item_row()])
    repo = GoldenExtractionItemRepository(_mk_conn(cur))
    item = GoldenExtractionItem(
        golden_set_id=UUID("22222222-2222-2222-2222-222222222222"),
        document_id=UUID("33333333-3333-3333-3333-333333333333"),
        document_type="REPORT",
        expected_fields=[ExpectedField(field_name="title", expected_value="T")],
        expected_spans=[],
        created_by="system",
    )
    saved = repo.create(item)
    assert saved.document_type == "REPORT"


def test_golden_item_list_by_set():
    cur = _mk_cur(fetchall_values=[[_mk_item_row()]])
    repo = GoldenExtractionItemRepository(_mk_conn(cur))
    result = repo.list_by_set(UUID("22222222-2222-2222-2222-222222222222"))
    assert len(result) == 1


def test_golden_item_row_to_item_json_strings():
    """expected_fields / expected_spans 가 DB에서 str 로 반환되는 경우."""
    repo = GoldenExtractionItemRepository(MagicMock())
    item = repo._row_to_item(_mk_item_row(fields_as_str=True, spans_as_str=True))
    assert len(item.expected_fields) == 1
    assert item.expected_fields[0].field_name == "title"


def test_golden_item_row_to_item_with_null_golden_set_id():
    repo = GoldenExtractionItemRepository(MagicMock())
    row = _mk_item_row()
    row["golden_set_id"] = None
    item = repo._row_to_item(row)
    assert item.golden_set_id is None
