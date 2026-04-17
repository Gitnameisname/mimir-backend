"""
ExtractionCandidateRepository 단위 테스트 — Phase 8 FG8.2

DB mock(MagicMock)으로 실제 DB 없이 Repository 로직을 검증한다.

테스트 범위:
  - create: 정상 생성
  - get_by_id: 조회, 없을 때 None
  - list_pending: scope 필터, 페이지네이션
  - count_pending: 건수 반환
  - list_by_status: 상태별 목록
  - list_by_document: 문서별 목록
  - update_status: approve/reject/modify
  - soft_delete: 소프트 삭제
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from app.models.extraction import ExtractionConfidenceScore, ExtractionMode, ExtractionStatus
from app.repositories.extraction_candidate_repository import ExtractionCandidateRepository

_NOW = datetime.now(timezone.utc)
_DOC_ID = uuid4()
_CAND_ID = uuid4()
_SCOPE_ID = uuid4()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_conn():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor
    return conn


@pytest.fixture
def repo(mock_conn):
    return ExtractionCandidateRepository(conn=mock_conn)


def _make_row(
    candidate_id=None,
    status="pending",
    scope_id=None,
    extracted_fields=None,
) -> dict:
    """테스트용 DB 행 딕셔너리."""
    return {
        "id": str(candidate_id or _CAND_ID),
        "document_id": str(_DOC_ID),
        "document_version": 1,
        "extraction_schema_id": "POLICY",
        "extraction_schema_version": 1,
        "extracted_fields": extracted_fields or {"title": "정책 1", "effective_date": "2024-01-01"},
        "confidence_scores": [{"field_name": "title", "confidence": 0.9, "reason": "단순 값"}],
        "extraction_model": "gpt-4o",
        "extraction_mode": "deterministic",
        "extraction_latency_ms": 120,
        "extraction_tokens": {"total": 350},
        "extraction_cost_estimate": None,
        "extraction_prompt_version": "abc12345",
        "document_content_hash": "deadbeef",
        "status": status,
        "reviewed_by": None,
        "reviewed_at": None,
        "human_feedback": None,
        "human_edits": [],
        "created_at": _NOW,
        "updated_at": _NOW,
        "actor_type": "agent",
        "scope_profile_id": str(scope_id or _SCOPE_ID),
        "is_soft_deleted": False,
    }


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

class TestCreate:
    def test_create_returns_candidate(self, repo, mock_conn):
        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = _make_row()

        scores = [ExtractionConfidenceScore(field_name="title", confidence=0.9, reason="단순 값")]
        result = repo.create(
            document_id=_DOC_ID,
            document_version=1,
            extraction_schema_id="POLICY",
            extraction_schema_version=1,
            extracted_fields={"title": "정책 1", "effective_date": "2024-01-01"},
            confidence_scores=scores,
            extraction_model="gpt-4o",
            extraction_mode=ExtractionMode.DETERMINISTIC,
            extraction_latency_ms=120,
            scope_profile_id=_SCOPE_ID,
        )

        assert result.extraction_schema_id == "POLICY"
        assert result.status == ExtractionStatus.PENDING
        assert result.actor_type == "agent"
        cursor.execute.assert_called_once()

    def test_create_inserts_correct_status(self, repo, mock_conn):
        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = _make_row()

        repo.create(
            document_id=_DOC_ID,
            document_version=1,
            extraction_schema_id="POLICY",
            extraction_schema_version=1,
            extracted_fields={"title": "x"},
            confidence_scores=[],
            extraction_model="local",
        )

        sql_call = cursor.execute.call_args[0][0]
        assert "pending" in sql_call.lower()


# ---------------------------------------------------------------------------
# get_by_id
# ---------------------------------------------------------------------------

class TestGetById:
    def test_get_by_id_found(self, repo, mock_conn):
        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = _make_row()

        result = repo.get_by_id(_CAND_ID)
        assert result is not None
        assert result.id == _CAND_ID

    def test_get_by_id_not_found(self, repo, mock_conn):
        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None

        result = repo.get_by_id(uuid4())
        assert result is None

    def test_get_by_id_returns_confidence_scores(self, repo, mock_conn):
        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = _make_row()

        result = repo.get_by_id(_CAND_ID)
        assert len(result.confidence_scores) == 1
        assert result.confidence_scores[0].field_name == "title"
        assert result.confidence_scores[0].confidence == 0.9


# ---------------------------------------------------------------------------
# list_pending
# ---------------------------------------------------------------------------

class TestListPending:
    def test_list_pending_no_scope_filter(self, repo, mock_conn):
        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [_make_row(), _make_row(candidate_id=uuid4())]

        results = repo.list_pending(limit=10, offset=0)
        assert len(results) == 2
        sql = cursor.execute.call_args[0][0]
        assert "status = 'pending'" in sql
        # scope_filter placeholder는 SQL 본문에 없어야 함 (SELECT 컬럼에는 있을 수 있음)
        assert "AND scope_profile_id" not in sql

    def test_list_pending_with_scope_filter(self, repo, mock_conn):
        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [_make_row()]

        results = repo.list_pending(scope_profile_id=_SCOPE_ID, limit=10, offset=0)
        assert len(results) == 1
        sql = cursor.execute.call_args[0][0]
        assert "scope_profile_id" in sql

    def test_list_pending_empty(self, repo, mock_conn):
        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []

        results = repo.list_pending()
        assert results == []


# ---------------------------------------------------------------------------
# count_pending
# ---------------------------------------------------------------------------

class TestCountPending:
    def test_count_pending(self, repo, mock_conn):
        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (5,)

        count = repo.count_pending()
        assert count == 5

    def test_count_pending_with_scope(self, repo, mock_conn):
        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (3,)

        count = repo.count_pending(scope_profile_id=_SCOPE_ID)
        assert count == 3
        sql = cursor.execute.call_args[0][0]
        assert "scope_profile_id" in sql


# ---------------------------------------------------------------------------
# list_by_status
# ---------------------------------------------------------------------------

class TestListByStatus:
    def test_list_approved(self, repo, mock_conn):
        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [_make_row(status="approved")]

        results = repo.list_by_status(ExtractionStatus.APPROVED)
        assert results[0].status == ExtractionStatus.APPROVED
        sql = cursor.execute.call_args[0][0]
        assert "status = %s" in sql

    def test_list_rejected_with_scope(self, repo, mock_conn):
        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []

        repo.list_by_status(ExtractionStatus.REJECTED, scope_profile_id=_SCOPE_ID)
        sql = cursor.execute.call_args[0][0]
        assert "scope_profile_id" in sql


# ---------------------------------------------------------------------------
# list_by_document
# ---------------------------------------------------------------------------

class TestListByDocument:
    def test_list_by_document(self, repo, mock_conn):
        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [_make_row()]

        results = repo.list_by_document(_DOC_ID)
        assert len(results) == 1
        sql = cursor.execute.call_args[0][0]
        assert "document_id = %s" in sql

    def test_list_by_document_with_version(self, repo, mock_conn):
        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [_make_row()]

        repo.list_by_document(_DOC_ID, document_version=1)
        sql = cursor.execute.call_args[0][0]
        assert "document_version" in sql


# ---------------------------------------------------------------------------
# update_status
# ---------------------------------------------------------------------------

class TestUpdateStatus:
    def test_approve_updates_status(self, repo, mock_conn):
        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = _make_row(status="approved")

        result = repo.update_status(
            _CAND_ID,
            new_status=ExtractionStatus.APPROVED,
            reviewed_by="user-abc",
        )
        assert result is not None
        assert result.status == ExtractionStatus.APPROVED

    def test_reject_sets_feedback(self, repo, mock_conn):
        cursor = mock_conn.cursor.return_value.__enter__.return_value
        row = _make_row(status="rejected")
        row["human_feedback"] = "잘못된 추출"
        cursor.fetchone.return_value = row

        result = repo.update_status(
            _CAND_ID,
            new_status=ExtractionStatus.REJECTED,
            reviewed_by="user-abc",
            human_feedback="잘못된 추출",
        )
        assert result.status == ExtractionStatus.REJECTED

    def test_update_status_not_found_returns_none(self, repo, mock_conn):
        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None

        result = repo.update_status(uuid4(), new_status=ExtractionStatus.APPROVED)
        assert result is None


# ---------------------------------------------------------------------------
# soft_delete
# ---------------------------------------------------------------------------

class TestSoftDelete:
    def test_soft_delete_success(self, repo, mock_conn):
        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1

        deleted = repo.soft_delete(_CAND_ID, "user-abc")
        assert deleted is True
        sql = cursor.execute.call_args[0][0]
        assert "is_soft_deleted = TRUE" in sql

    def test_soft_delete_not_found(self, repo, mock_conn):
        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cursor.rowcount = 0

        deleted = repo.soft_delete(uuid4(), "user-abc")
        assert deleted is False
