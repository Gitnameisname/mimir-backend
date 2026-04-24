"""FG 0-3 커버리지 보강 — batch_extraction_repository 유닛 테스트 (세션 13-B).

대상: `backend/app/repositories/batch_extraction_repository.py` (276줄)

커버 범위:
  - _row_to_job 기본/defaults
  - BatchExtractionJobRepository.create / get_by_id (found/None)
  - list_by_scope (no scope+status / with scope / with status)
  - update_status (RUNNING COALESCE / 종료 상태 completed_at / error_summary / None)
  - update_progress (기본 / total=0 division guard)
  - append_failed_document
  - request_cancellation (True/False)
  - is_cancellation_requested (True/False/None row)
  - ExtractionRetryLogRepository.create + list_by_job
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from app.repositories.batch_extraction_repository import (
    BatchExtractionJobRepository,
    ExtractionRetryLogRepository,
    _row_to_job,
)
from app.models.batch_extraction import BatchJobStatus


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
    # RealDictCursor 사용하는 코드도 동일 cur 반환
    conn.cursor = MagicMock(return_value=cur)
    return conn


def _mk_job_row(
    status="pending",
    total=10,
    failed_document_ids=None,
    sample_mode=False,
    comparison_mode=False,
    is_cancellation_requested=False,
    scope_profile_id=None,
):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "extraction_schema_id": "REPORT",
        "extraction_schema_version": 1,
        "scope_profile_id": scope_profile_id,
        "status": status,
        "total_count": total,
        "completed_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "progress_percentage": 0.0,
        "date_from": None,
        "date_to": None,
        "sample_count": None,
        "sample_mode": sample_mode,
        "comparison_mode": comparison_mode,
        "comparison_report_path": None,
        "created_at": now,
        "started_at": None,
        "completed_at": None,
        "estimated_completion_at": None,
        "current_processing": None,
        "error_summary": None,
        "failed_document_ids": failed_document_ids,
        "created_by": "user-1",
        "is_cancellation_requested": is_cancellation_requested,
        "actor_type": "user",
    }


# ---------------------------------------------------------------------------
# 1. _row_to_job
# ---------------------------------------------------------------------------


def test_row_to_job_happy_path():
    job = _row_to_job(_mk_job_row())
    assert job.total_count == 10
    assert job.status == BatchJobStatus.PENDING
    assert job.failed_document_ids == []


def test_row_to_job_with_scope():
    sid = str(uuid4())
    job = _row_to_job(_mk_job_row(scope_profile_id=sid))
    assert job.scope_profile_id is not None


def test_row_to_job_defaults_missing_actor_type():
    row = _mk_job_row()
    del row["actor_type"]
    job = _row_to_job(row)
    assert job.actor_type == "user"


# ---------------------------------------------------------------------------
# 2. create
# ---------------------------------------------------------------------------


def test_create_job_basic():
    cur = _mk_cur(fetchone_values=[_mk_job_row()])
    repo = BatchExtractionJobRepository(_mk_conn(cur))
    result = repo.create(
        extraction_schema_id="REPORT",
        extraction_schema_version=1,
        total_count=10,
        created_by="user-1",
    )
    assert result.total_count == 10


def test_create_job_with_scope_and_sample_mode():
    cur = _mk_cur(fetchone_values=[_mk_job_row(sample_mode=True)])
    repo = BatchExtractionJobRepository(_mk_conn(cur))
    sid = uuid4()
    repo.create(
        extraction_schema_id="REPORT",
        extraction_schema_version=1,
        total_count=100,
        created_by="user-1",
        scope_profile_id=sid,
        sample_count=20,
        sample_mode=True,
        comparison_mode=True,
        actor_type="agent",
    )
    params_dict = cur.execute.call_args[0][1]
    assert params_dict["scope"] == str(sid)
    assert params_dict["sample_mode"] is True
    assert params_dict["actor_type"] == "agent"


# ---------------------------------------------------------------------------
# 3. get_by_id / list_by_scope
# ---------------------------------------------------------------------------


def test_get_by_id_found():
    cur = _mk_cur(fetchone_values=[_mk_job_row()])
    repo = BatchExtractionJobRepository(_mk_conn(cur))
    result = repo.get_by_id(UUID("11111111-1111-1111-1111-111111111111"))
    assert result is not None


def test_get_by_id_not_found():
    cur = _mk_cur(fetchone_values=[None])
    repo = BatchExtractionJobRepository(_mk_conn(cur))
    assert repo.get_by_id(UUID("11111111-1111-1111-1111-111111111111")) is None


def test_list_by_scope_no_filters():
    cur = _mk_cur(fetchall_values=[[_mk_job_row()]])
    repo = BatchExtractionJobRepository(_mk_conn(cur))
    result = repo.list_by_scope(None)
    assert len(result) == 1


def test_list_by_scope_with_scope_and_status():
    cur = _mk_cur(fetchall_values=[[]])
    repo = BatchExtractionJobRepository(_mk_conn(cur))
    sid = uuid4()
    repo.list_by_scope(sid, status=BatchJobStatus.RUNNING)
    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]
    assert "scope_profile_id = %s" in sql
    assert "status = %s" in sql
    assert str(sid) in params
    assert "running" in params


# ---------------------------------------------------------------------------
# 4. update_status
# ---------------------------------------------------------------------------


def test_update_status_running_coalesces_started_at():
    cur = _mk_cur(fetchone_values=[_mk_job_row(status="running")])
    repo = BatchExtractionJobRepository(_mk_conn(cur))
    repo.update_status(
        UUID("11111111-1111-1111-1111-111111111111"),
        BatchJobStatus.RUNNING,
    )
    sql = cur.execute.call_args[0][0]
    assert "started_at = COALESCE(started_at" in sql


def test_update_status_completed_sets_completed_at():
    cur = _mk_cur(fetchone_values=[_mk_job_row(status="completed")])
    repo = BatchExtractionJobRepository(_mk_conn(cur))
    repo.update_status(
        UUID("11111111-1111-1111-1111-111111111111"),
        BatchJobStatus.COMPLETED,
    )
    sql = cur.execute.call_args[0][0]
    assert "completed_at = %(completed_at)s" in sql


def test_update_status_failed_records_error_summary():
    cur = _mk_cur(fetchone_values=[_mk_job_row(status="failed")])
    repo = BatchExtractionJobRepository(_mk_conn(cur))
    repo.update_status(
        UUID("11111111-1111-1111-1111-111111111111"),
        BatchJobStatus.FAILED,
        error_summary="quota exceeded",
    )
    params = cur.execute.call_args[0][1]
    assert params["error_summary"] == "quota exceeded"


def test_update_status_not_found_returns_none():
    cur = _mk_cur(fetchone_values=[None])
    repo = BatchExtractionJobRepository(_mk_conn(cur))
    result = repo.update_status(
        UUID("11111111-1111-1111-1111-111111111111"),
        BatchJobStatus.COMPLETED,
    )
    assert result is None


# ---------------------------------------------------------------------------
# 5. update_progress
# ---------------------------------------------------------------------------


def test_update_progress_basic():
    cur = _mk_cur(fetchone_values=[_mk_job_row()])
    repo = BatchExtractionJobRepository(_mk_conn(cur))
    repo.update_progress(
        UUID("11111111-1111-1111-1111-111111111111"),
        completed=5,
        failed=1,
        skipped=0,
        total=10,
    )
    params = cur.execute.call_args[0][1]
    # (5+1+0)/10 = 60.0%
    assert params["progress"] == 60.0
    assert params["completed"] == 5
    assert params["failed"] == 1


def test_update_progress_zero_total_guards_division():
    cur = _mk_cur(fetchone_values=[_mk_job_row()])
    repo = BatchExtractionJobRepository(_mk_conn(cur))
    repo.update_progress(
        UUID("11111111-1111-1111-1111-111111111111"),
        completed=0, failed=0, skipped=0, total=0,
    )
    params = cur.execute.call_args[0][1]
    assert params["progress"] == 0.0


def test_update_progress_not_found_returns_none():
    cur = _mk_cur(fetchone_values=[None])
    repo = BatchExtractionJobRepository(_mk_conn(cur))
    result = repo.update_progress(
        UUID("11111111-1111-1111-1111-111111111111"),
        completed=0, failed=0, skipped=0, total=10,
    )
    assert result is None


# ---------------------------------------------------------------------------
# 6. append_failed_document
# ---------------------------------------------------------------------------


def test_append_failed_document_executes_update():
    cur = _mk_cur()
    repo = BatchExtractionJobRepository(_mk_conn(cur))
    doc_id = UUID("22222222-2222-2222-2222-222222222222")
    repo.append_failed_document(
        UUID("11111111-1111-1111-1111-111111111111"), doc_id
    )
    sql = cur.execute.call_args[0][0]
    assert "failed_document_ids = failed_document_ids ||" in sql
    params = cur.execute.call_args[0][1]
    assert str(doc_id) in params["doc_id"]


# ---------------------------------------------------------------------------
# 7. request_cancellation / is_cancellation_requested
# ---------------------------------------------------------------------------


def test_request_cancellation_true_when_row_returned():
    cur = _mk_cur(fetchone_values=[{"id": "11111111-1111-1111-1111-111111111111"}])
    repo = BatchExtractionJobRepository(_mk_conn(cur))
    assert repo.request_cancellation(
        UUID("11111111-1111-1111-1111-111111111111")
    ) is True


def test_request_cancellation_false_when_no_row():
    cur = _mk_cur(fetchone_values=[None])
    repo = BatchExtractionJobRepository(_mk_conn(cur))
    assert repo.request_cancellation(
        UUID("11111111-1111-1111-1111-111111111111")
    ) is False


def test_is_cancellation_requested_true():
    cur = _mk_cur(fetchone_values=[{"is_cancellation_requested": True}])
    repo = BatchExtractionJobRepository(_mk_conn(cur))
    assert repo.is_cancellation_requested(
        UUID("11111111-1111-1111-1111-111111111111")
    ) is True


def test_is_cancellation_requested_false():
    cur = _mk_cur(fetchone_values=[{"is_cancellation_requested": False}])
    repo = BatchExtractionJobRepository(_mk_conn(cur))
    assert repo.is_cancellation_requested(
        UUID("11111111-1111-1111-1111-111111111111")
    ) is False


def test_is_cancellation_requested_returns_false_when_row_none():
    cur = _mk_cur(fetchone_values=[None])
    repo = BatchExtractionJobRepository(_mk_conn(cur))
    assert repo.is_cancellation_requested(
        UUID("11111111-1111-1111-1111-111111111111")
    ) is False


# ---------------------------------------------------------------------------
# 8. ExtractionRetryLogRepository
# ---------------------------------------------------------------------------


def test_retry_log_create_executes_insert():
    cur = _mk_cur()
    repo = ExtractionRetryLogRepository(_mk_conn(cur))
    repo.create(
        job_id=UUID("11111111-1111-1111-1111-111111111111"),
        document_id=UUID("22222222-2222-2222-2222-222222222222"),
        attempt_number=2,
        status="failed",
        error_reason="timeout",
        latency_ms=500,
    )
    sql = cur.execute.call_args[0][0]
    assert "INSERT INTO extraction_retry_logs" in sql
    params = cur.execute.call_args[0][1]
    # 위치 파라미터로 전달
    assert 2 in params
    assert "failed" in params
    assert "timeout" in params


def test_retry_log_list_by_job_returns_dicts():
    now = datetime.now(timezone.utc)
    rows = [
        {"id": "a", "job_id": "j", "document_id": "d", "attempt_number": 1,
         "status": "failed", "error_reason": "err", "latency_ms": 100,
         "created_at": now},
    ]
    cur = _mk_cur(fetchall_values=[rows])
    repo = ExtractionRetryLogRepository(_mk_conn(cur))
    result = repo.list_by_job(UUID("11111111-1111-1111-1111-111111111111"))
    assert len(result) == 1
    assert result[0]["attempt_number"] == 1
