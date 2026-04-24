"""FG 0-3 커버리지 보강 — extraction_candidate_repository 유닛 테스트 (세션 12-B).

대상: `backend/app/repositories/extraction_candidate_repository.py` (506줄)

커버 범위:
  - _row_to_candidate (dict/str JSON 파싱, defaults, human_edits/confidence_scores 정규화)
  - create (기본 / scope_profile_id / extraction_tokens 포함)
  - get_by_id (found/None)
  - list_pending (기본 / scope)
  - count_pending (기본 / scope)
  - list_by_status (기본 / scope)
  - list_by_document (version 필터 유무)
  - list_for_admin_queue (statuses/document_type/scope 필터 조합, total count 동시 반환)
  - get_for_admin_detail (found/None)
  - update_status (found/None + edits JSON 직렬화)
  - soft_delete (True/False)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from app.repositories.extraction_candidate_repository import (
    ExtractionCandidateRepository,
)
from app.models.extraction import (
    ExtractionConfidenceScore,
    ExtractionMode,
    ExtractionStatus,
    HumanEditRecord,
)


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------


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


def _mk_row(
    id_="11111111-1111-1111-1111-111111111111",
    document_id="22222222-2222-2222-2222-222222222222",
    status="pending",
    scope_profile_id=None,
    confidence_scores=None,
    human_edits=None,
    extracted_fields=None,
):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    return {
        "id": id_,
        "document_id": document_id,
        "document_version": 1,
        "extraction_schema_id": "REPORT",
        "extraction_schema_version": 1,
        "extracted_fields": extracted_fields if extracted_fields is not None else {"title": "T"},
        "confidence_scores": confidence_scores if confidence_scores is not None else [],
        "extraction_model": "gpt-4o",
        "extraction_mode": "deterministic",
        "extraction_latency_ms": 123,
        "extraction_tokens": None,
        "extraction_cost_estimate": None,
        "extraction_prompt_version": None,
        "document_content_hash": None,
        "status": status,
        "reviewed_by": None,
        "reviewed_at": None,
        "human_feedback": None,
        "human_edits": human_edits if human_edits is not None else [],
        "created_at": now,
        "updated_at": now,
        "actor_type": "agent",
        "scope_profile_id": scope_profile_id,
        "is_soft_deleted": False,
    }


# ---------------------------------------------------------------------------
# 1. _row_to_candidate
# ---------------------------------------------------------------------------


def test_row_to_candidate_happy_path():
    repo = ExtractionCandidateRepository(MagicMock())
    row = _mk_row()
    c = repo._row_to_candidate(row)
    assert c.extraction_model == "gpt-4o"
    assert c.status == ExtractionStatus("pending")
    assert c.is_soft_deleted is False


def test_row_to_candidate_parses_json_strings():
    """DB 가 str 로 반환하는 경우 json.loads 분기."""
    repo = ExtractionCandidateRepository(MagicMock())
    row = _mk_row(
        confidence_scores=json.dumps([{"field_name": "title", "confidence": 0.9}]),
        human_edits=json.dumps([]),
        extracted_fields=json.dumps({"title": "T"}),
    )
    c = repo._row_to_candidate(row)
    assert len(c.confidence_scores) == 1
    assert isinstance(c.confidence_scores[0], ExtractionConfidenceScore)


def test_row_to_candidate_with_scope():
    repo = ExtractionCandidateRepository(MagicMock())
    row = _mk_row(scope_profile_id="99999999-9999-9999-9999-999999999999")
    c = repo._row_to_candidate(row)
    assert c.scope_profile_id is not None


def test_row_to_candidate_defaults_when_missing_optional_fields():
    repo = ExtractionCandidateRepository(MagicMock())
    row = _mk_row()
    # 선택 필드 제거
    del row["extraction_mode"]
    del row["status"]
    del row["actor_type"]
    del row["is_soft_deleted"]
    c = repo._row_to_candidate(row)
    assert c.extraction_mode == ExtractionMode("deterministic")
    assert c.status == ExtractionStatus("pending")
    assert c.actor_type == "agent"


# ---------------------------------------------------------------------------
# 2. create
# ---------------------------------------------------------------------------


def test_create_basic():
    cur = _mk_cur(fetchone_values=[_mk_row()])
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    result = repo.create(
        document_id=UUID("22222222-2222-2222-2222-222222222222"),
        document_version=1,
        extraction_schema_id="REPORT",
        extraction_schema_version=1,
        extracted_fields={"title": "T"},
        confidence_scores=[ExtractionConfidenceScore(field_name="title", confidence=0.9)],
        extraction_model="gpt-4o",
    )
    assert result.extraction_model == "gpt-4o"
    assert cur.execute.call_count == 1


def test_create_with_scope_and_tokens():
    cur = _mk_cur(fetchone_values=[_mk_row()])
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    sid = uuid4()
    repo.create(
        document_id=UUID("22222222-2222-2222-2222-222222222222"),
        document_version=1,
        extraction_schema_id="REPORT",
        extraction_schema_version=1,
        extracted_fields={"title": "T"},
        confidence_scores=[],
        extraction_model="gpt-4o",
        extraction_tokens={"input": 100, "output": 50},
        extraction_cost_estimate=0.01,
        scope_profile_id=sid,
        actor_type="user",
    )
    params = cur.execute.call_args[0][1]
    assert str(sid) in params
    assert "user" in params
    # tokens JSON 이 파라미터에 포함됨
    assert any("input" in p for p in params if isinstance(p, str))


# ---------------------------------------------------------------------------
# 3. get_by_id
# ---------------------------------------------------------------------------


def test_get_by_id_found():
    cur = _mk_cur(fetchone_values=[_mk_row()])
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    result = repo.get_by_id(UUID("11111111-1111-1111-1111-111111111111"))
    assert result is not None
    sql = cur.execute.call_args[0][0]
    assert "is_soft_deleted = FALSE" in sql


def test_get_by_id_not_found():
    cur = _mk_cur(fetchone_values=[None])
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    assert repo.get_by_id(UUID("11111111-1111-1111-1111-111111111111")) is None


# ---------------------------------------------------------------------------
# 4. list_pending + count_pending
# ---------------------------------------------------------------------------


def test_list_pending_basic():
    cur = _mk_cur(fetchall_values=[[_mk_row(status="pending")]])
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    result = repo.list_pending()
    assert len(result) == 1
    sql = cur.execute.call_args[0][0]
    assert "status = 'pending'" in sql


def test_list_pending_with_scope_filter():
    cur = _mk_cur(fetchall_values=[[]])
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    sid = uuid4()
    repo.list_pending(scope_profile_id=sid, limit=10, offset=5)
    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]
    assert "scope_profile_id = %s" in sql
    assert str(sid) in params
    # limit/offset 도 포함
    assert 10 in params and 5 in params


def test_count_pending_with_scope():
    cur = _mk_cur(fetchone_values=[{"count": 42}])
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    sid = uuid4()
    result = repo.count_pending(scope_profile_id=sid)
    assert result == 42


def test_count_pending_returns_zero_when_none():
    cur = _mk_cur(fetchone_values=[None])
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    assert repo.count_pending() == 0


# ---------------------------------------------------------------------------
# 5. list_by_status
# ---------------------------------------------------------------------------


def test_list_by_status_basic():
    cur = _mk_cur(fetchall_values=[[_mk_row(status="approved")]])
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    result = repo.list_by_status(ExtractionStatus.APPROVED)
    assert len(result) == 1


def test_list_by_status_with_scope():
    cur = _mk_cur(fetchall_values=[[]])
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    sid = uuid4()
    repo.list_by_status(ExtractionStatus.PENDING, scope_profile_id=sid)
    params = cur.execute.call_args[0][1]
    assert str(sid) in params


# ---------------------------------------------------------------------------
# 6. list_by_document
# ---------------------------------------------------------------------------


def test_list_by_document_without_version_filter():
    cur = _mk_cur(fetchall_values=[[_mk_row()]])
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    result = repo.list_by_document(UUID("22222222-2222-2222-2222-222222222222"))
    assert len(result) == 1
    sql = cur.execute.call_args[0][0]
    # version_filter 비어있어야 함
    assert "AND document_version = %s" not in sql


def test_list_by_document_with_version_filter():
    cur = _mk_cur(fetchall_values=[[_mk_row()]])
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    repo.list_by_document(
        UUID("22222222-2222-2222-2222-222222222222"),
        document_version=3,
    )
    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]
    assert "document_version = %s" in sql
    assert 3 in params


# ---------------------------------------------------------------------------
# 7. list_for_admin_queue
# ---------------------------------------------------------------------------


def test_list_for_admin_queue_basic():
    admin_row = dict(_mk_row())
    admin_row["document_title"] = "문서 제목"
    admin_row["document_summary"] = "요약"
    admin_row["document_document_type"] = "REPORT"

    cur = _mk_cur(
        fetchall_values=[[admin_row]],
        fetchone_values=[{"total": 5}],
    )
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    rows, total = repo.list_for_admin_queue()
    assert len(rows) == 1
    assert rows[0]["document_title"] == "문서 제목"
    assert total == 5


def test_list_for_admin_queue_with_statuses_filter():
    cur = _mk_cur(
        fetchall_values=[[]],
        fetchone_values=[{"total": 0}],
    )
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    repo.list_for_admin_queue(
        statuses=[ExtractionStatus.PENDING, ExtractionStatus.APPROVED]
    )
    # 첫 번째 execute 는 data 쿼리
    data_params = cur.execute.call_args_list[0][0][1]
    # statuses 리스트가 ANY(%s) 로 전달
    assert any(isinstance(p, list) for p in data_params)


def test_list_for_admin_queue_with_document_type():
    cur = _mk_cur(
        fetchall_values=[[]],
        fetchone_values=[{"total": 0}],
    )
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    repo.list_for_admin_queue(document_type="REPORT")
    data_sql = cur.execute.call_args_list[0][0][0]
    data_params = cur.execute.call_args_list[0][0][1]
    assert "c.extraction_schema_id = %s" in data_sql
    assert "REPORT" in data_params


def test_list_for_admin_queue_with_scope_filter():
    cur = _mk_cur(
        fetchall_values=[[]],
        fetchone_values=[{"total": 0}],
    )
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    sid = uuid4()
    repo.list_for_admin_queue(scope_profile_id=sid)
    params = cur.execute.call_args_list[0][0][1]
    assert str(sid) in params


def test_list_for_admin_queue_total_zero_when_count_none():
    cur = _mk_cur(
        fetchall_values=[[]],
        fetchone_values=[None],
    )
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    rows, total = repo.list_for_admin_queue()
    assert rows == []
    assert total == 0


# ---------------------------------------------------------------------------
# 8. get_for_admin_detail
# ---------------------------------------------------------------------------


def test_get_for_admin_detail_found():
    admin_row = dict(_mk_row())
    admin_row["document_title"] = "제목"
    admin_row["document_summary"] = "요약"
    admin_row["document_document_type"] = "REPORT"
    cur = _mk_cur(fetchone_values=[admin_row])
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    result = repo.get_for_admin_detail(
        UUID("11111111-1111-1111-1111-111111111111")
    )
    assert result is not None
    assert result["document_title"] == "제목"


def test_get_for_admin_detail_not_found():
    cur = _mk_cur(fetchone_values=[None])
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    result = repo.get_for_admin_detail(
        UUID("11111111-1111-1111-1111-111111111111")
    )
    assert result is None


# ---------------------------------------------------------------------------
# 9. update_status
# ---------------------------------------------------------------------------


def test_update_status_success():
    cur = _mk_cur(fetchone_values=[_mk_row(status="approved")])
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    result = repo.update_status(
        UUID("11111111-1111-1111-1111-111111111111"),
        new_status=ExtractionStatus.APPROVED,
        reviewed_by="reviewer-1",
        human_feedback="looks good",
        human_edits=[
            HumanEditRecord(
                field_name="title",
                before_value="A",
                after_value="B",
                edited_at=datetime(2026, 4, 24, tzinfo=timezone.utc),
                edited_by="reviewer-1",
            )
        ],
    )
    assert result is not None
    assert result.status == ExtractionStatus.APPROVED


def test_update_status_not_found_returns_none():
    cur = _mk_cur(fetchone_values=[None])
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    result = repo.update_status(
        UUID("11111111-1111-1111-1111-111111111111"),
        new_status=ExtractionStatus.REJECTED,
    )
    assert result is None


def test_update_status_empty_edits_serializes_empty_list():
    cur = _mk_cur(fetchone_values=[_mk_row()])
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    repo.update_status(
        UUID("11111111-1111-1111-1111-111111111111"),
        new_status=ExtractionStatus.APPROVED,
    )
    # 파라미터에 빈 JSON 리스트 포함
    params = cur.execute.call_args[0][1]
    assert "[]" in params


# ---------------------------------------------------------------------------
# 10. soft_delete
# ---------------------------------------------------------------------------


def test_soft_delete_returns_true_on_update():
    cur = _mk_cur(rowcount=1)
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    assert repo.soft_delete(
        UUID("11111111-1111-1111-1111-111111111111"), "admin-1"
    ) is True


def test_soft_delete_returns_false_when_not_found():
    cur = _mk_cur(rowcount=0)
    repo = ExtractionCandidateRepository(_mk_conn(cur))
    assert repo.soft_delete(
        UUID("11111111-1111-1111-1111-111111111111"), "admin-1"
    ) is False
