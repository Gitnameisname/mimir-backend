"""
배치 재추출 서비스 단위 테스트 — Phase 8 Task 8-7.

asyncio.run() 패턴 사용 (pytest-asyncio 미설치 환경 호환).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.models.batch_extraction import BatchExtractionJob, BatchJobStatus


# ---------------------------------------------------------------------------
# BatchExtractionJobRepository 단위 테스트
# ---------------------------------------------------------------------------

class TestBatchExtractionJobRepository:
    def _make_conn(self, row=None, rowcount=1):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = row
        cur.fetchall.return_value = [row] if row else []
        cur.rowcount = rowcount
        conn.cursor.return_value = cur
        return conn

    def _make_row(self, job_id=None, status="pending"):
        now = datetime.now(timezone.utc)
        return {
            "id": str(job_id or uuid4()),
            "extraction_schema_id": "POLICY",
            "extraction_schema_version": 1,
            "scope_profile_id": None,
            "status": status,
            "total_count": 10,
            "completed_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "progress_percentage": 0.0,
            "date_from": None,
            "date_to": None,
            "sample_count": None,
            "sample_mode": False,
            "comparison_mode": False,
            "comparison_report_path": None,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "estimated_completion_at": None,
            "current_processing": None,
            "error_summary": None,
            "failed_document_ids": [],
            "created_by": "user-abc",
            "is_cancellation_requested": False,
            "actor_type": "user",
        }

    def test_create_returns_job(self):
        from app.repositories.batch_extraction_repository import BatchExtractionJobRepository
        row = self._make_row()
        conn = self._make_conn(row=row)
        repo = BatchExtractionJobRepository(conn)
        job = repo.create(
            extraction_schema_id="POLICY",
            extraction_schema_version=1,
            total_count=10,
            created_by="user-abc",
        )
        assert job.extraction_schema_id == "POLICY"
        assert job.status == BatchJobStatus.PENDING

    def test_get_by_id_found(self):
        from app.repositories.batch_extraction_repository import BatchExtractionJobRepository
        job_id = uuid4()
        row = self._make_row(job_id=job_id)
        conn = self._make_conn(row=row)
        repo = BatchExtractionJobRepository(conn)
        result = repo.get_by_id(job_id)
        assert result is not None
        assert result.id == job_id

    def test_get_by_id_not_found(self):
        from app.repositories.batch_extraction_repository import BatchExtractionJobRepository
        conn = self._make_conn(row=None)
        repo = BatchExtractionJobRepository(conn)
        result = repo.get_by_id(uuid4())
        assert result is None

    def test_update_status_to_running(self):
        from app.repositories.batch_extraction_repository import BatchExtractionJobRepository
        job_id = uuid4()
        row = self._make_row(job_id=job_id, status="running")
        conn = self._make_conn(row=row)
        repo = BatchExtractionJobRepository(conn)
        result = repo.update_status(job_id, BatchJobStatus.RUNNING)
        assert result is not None
        assert result.status == BatchJobStatus.RUNNING

    def test_update_status_to_cancelled(self):
        from app.repositories.batch_extraction_repository import BatchExtractionJobRepository
        job_id = uuid4()
        row = self._make_row(job_id=job_id, status="cancelled")
        conn = self._make_conn(row=row)
        repo = BatchExtractionJobRepository(conn)
        result = repo.update_status(job_id, BatchJobStatus.CANCELLED)
        assert result.status == BatchJobStatus.CANCELLED

    def test_update_status_not_found(self):
        from app.repositories.batch_extraction_repository import BatchExtractionJobRepository
        conn = self._make_conn(row=None)
        repo = BatchExtractionJobRepository(conn)
        result = repo.update_status(uuid4(), BatchJobStatus.COMPLETED)
        assert result is None

    def test_update_progress_calculates_percentage(self):
        from app.repositories.batch_extraction_repository import BatchExtractionJobRepository
        job_id = uuid4()
        row = self._make_row(job_id=job_id)
        row["completed_count"] = 5
        row["progress_percentage"] = 50.0
        conn = self._make_conn(row=row)
        repo = BatchExtractionJobRepository(conn)
        result = repo.update_progress(job_id, completed=5, failed=0, skipped=0, total=10)
        assert result is not None
        # SQL에서 progress=50.0으로 업데이트됨
        sql_call = conn.cursor.return_value.__enter__.return_value.execute.call_args[0][0]
        assert "progress_percentage" in sql_call

    def test_request_cancellation_success(self):
        from app.repositories.batch_extraction_repository import BatchExtractionJobRepository
        job_id = uuid4()
        conn = self._make_conn(row=(str(job_id),))
        # rowcount=1 의미
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (str(job_id),)
        repo = BatchExtractionJobRepository(conn)
        result = repo.request_cancellation(job_id)
        assert result is True

    def test_request_cancellation_not_found(self):
        from app.repositories.batch_extraction_repository import BatchExtractionJobRepository
        conn = self._make_conn(row=None)
        repo = BatchExtractionJobRepository(conn)
        result = repo.request_cancellation(uuid4())
        assert result is False

    def test_is_cancellation_requested_true(self):
        from app.repositories.batch_extraction_repository import BatchExtractionJobRepository
        conn = self._make_conn(row=(True,))
        repo = BatchExtractionJobRepository(conn)
        assert repo.is_cancellation_requested(uuid4()) is True

    def test_is_cancellation_requested_false(self):
        from app.repositories.batch_extraction_repository import BatchExtractionJobRepository
        conn = self._make_conn(row=(False,))
        repo = BatchExtractionJobRepository(conn)
        assert repo.is_cancellation_requested(uuid4()) is False


# ---------------------------------------------------------------------------
# BatchExtractionJob 도메인 모델 검증
# ---------------------------------------------------------------------------

class TestBatchExtractionJobModel:
    def test_valid_model(self):
        from datetime import datetime, timezone
        job = BatchExtractionJob(
            id=uuid4(),
            extraction_schema_id="POLICY",
            extraction_schema_version=1,
            total_count=100,
            created_by="user-abc",
            created_at=datetime.now(timezone.utc),
        )
        assert job.status == BatchJobStatus.PENDING
        assert job.progress_percentage == 0.0

    def test_negative_count_raises(self):
        from datetime import datetime, timezone
        with pytest.raises(Exception):
            BatchExtractionJob(
                id=uuid4(),
                extraction_schema_id="POLICY",
                extraction_schema_version=1,
                total_count=10,
                completed_count=-1,
                created_by="user-abc",
                created_at=datetime.now(timezone.utc),
            )

    def test_progress_out_of_range_raises(self):
        from datetime import datetime, timezone
        with pytest.raises(Exception):
            BatchExtractionJob(
                id=uuid4(),
                extraction_schema_id="POLICY",
                extraction_schema_version=1,
                total_count=10,
                progress_percentage=150.0,
                created_by="user-abc",
                created_at=datetime.now(timezone.utc),
            )


# ---------------------------------------------------------------------------
# BatchExtractionJobResponse DTO
# ---------------------------------------------------------------------------

class TestBatchExtractionJobResponse:
    def test_from_domain(self):
        from datetime import datetime, timezone
        from app.models.batch_extraction import BatchExtractionJobResponse
        job = BatchExtractionJob(
            id=uuid4(),
            extraction_schema_id="POLICY",
            extraction_schema_version=1,
            total_count=20,
            completed_count=5,
            failed_count=1,
            skipped_count=0,
            progress_percentage=30.0,
            created_by="user-xyz",
            created_at=datetime.now(timezone.utc),
        )
        resp = BatchExtractionJobResponse.from_domain(job)
        assert resp.extraction_schema_id == "POLICY"
        assert resp.progress_percentage == 30.0
        assert resp.failed_count == 1
        assert resp.scope_profile_id is None


# ---------------------------------------------------------------------------
# 배치 워커 서비스 — 청크 처리 로직
# ---------------------------------------------------------------------------

class TestBatchWorkerChunkProcessing:
    def test_process_chunk_all_success(self):
        """청크 내 모든 문서 추출 성공 시 completed_count 증가."""
        from app.services.extraction.batch_extraction_service import _process_chunk

        call_count = [0]
        async def mock_re_extract(**kwargs):
            call_count[0] += 1

        doc_ids = [uuid4(), uuid4(), uuid4()]
        with patch(
            "app.services.extraction.batch_extraction_service._re_extract_document",
            side_effect=mock_re_extract,
        ), patch(
            "app.services.extraction.batch_extraction_service._log_retry",
        ), patch(
            "app.services.extraction.batch_extraction_service._mark_failed_document",
        ):
            result = asyncio.run(_process_chunk(
                job_id=uuid4(),
                document_ids=doc_ids,
                extraction_schema_id="POLICY",
                extraction_schema_version=1,
                scope_profile_id=None,
                llm_provider=MagicMock(),
            ))

        assert result["completed"] == 3
        assert result["failed"] == 0

    def test_process_chunk_partial_failure(self):
        """첫 번째 문서가 MAX_RETRIES 소진 후 failed → 나머지 2개 성공."""
        from app.services.extraction.batch_extraction_service import _process_chunk, MAX_RETRIES

        doc_ids = [uuid4(), uuid4(), uuid4()]
        first_doc = doc_ids[0]

        async def mock_re_extract(*, document_id, **kwargs):
            if document_id == first_doc:
                raise Exception("always fail for first doc")

        with patch(
            "app.services.extraction.batch_extraction_service._re_extract_document",
            side_effect=mock_re_extract,
        ), patch(
            "app.services.extraction.batch_extraction_service._log_retry",
        ), patch(
            "app.services.extraction.batch_extraction_service._mark_failed_document",
        ), patch(
            "app.services.extraction.batch_extraction_service.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ):
            result = asyncio.run(_process_chunk(
                job_id=uuid4(),
                document_ids=doc_ids,
                extraction_schema_id="POLICY",
                extraction_schema_version=1,
                scope_profile_id=None,
                llm_provider=MagicMock(),
            ))

        # 첫 문서는 MAX_RETRIES 소진 후 failed, 나머지 2개 성공
        assert result["failed"] == 1
        assert result["completed"] == 2

    def test_process_chunk_empty(self):
        """빈 청크는 즉시 반환."""
        from app.services.extraction.batch_extraction_service import _process_chunk
        result = asyncio.run(_process_chunk(
            job_id=uuid4(),
            document_ids=[],
            extraction_schema_id="POLICY",
            extraction_schema_version=1,
            scope_profile_id=None,
            llm_provider=MagicMock(),
        ))
        assert result == {"completed": 0, "failed": 0, "skipped": 0}

    def test_process_chunk_all_fail(self):
        """모든 문서 추출 실패 → failed == len(doc_ids)."""
        from app.services.extraction.batch_extraction_service import _process_chunk

        async def always_fail(**kwargs):
            raise Exception("always fail")

        doc_ids = [uuid4(), uuid4()]
        with patch(
            "app.services.extraction.batch_extraction_service._re_extract_document",
            side_effect=always_fail,
        ), patch(
            "app.services.extraction.batch_extraction_service._log_retry",
        ), patch(
            "app.services.extraction.batch_extraction_service._mark_failed_document",
        ), patch("asyncio.sleep", return_value=None):
            result = asyncio.run(_process_chunk(
                job_id=uuid4(),
                document_ids=doc_ids,
                extraction_schema_id="POLICY",
                extraction_schema_version=1,
                scope_profile_id=None,
                llm_provider=MagicMock(),
            ))
        assert result["failed"] == 2
        assert result["completed"] == 0


# ---------------------------------------------------------------------------
# ExtractionRetryLogRepository
# ---------------------------------------------------------------------------

class TestExtractionRetryLogRepository:
    def test_create_log(self):
        from app.repositories.batch_extraction_repository import ExtractionRetryLogRepository
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur

        repo = ExtractionRetryLogRepository(conn)
        repo.create(
            job_id=uuid4(),
            document_id=uuid4(),
            attempt_number=1,
            status="success",
            error_reason=None,
            latency_ms=120,
        )
        cur.execute.assert_called_once()
        sql = cur.execute.call_args[0][0]
        assert "extraction_retry_logs" in sql
