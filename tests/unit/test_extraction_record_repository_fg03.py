"""FG 0-3 커버리지 보강 — extraction_record_repository 유닛 테스트 (세션 13-D).

대상: `backend/app/repositories/extraction_record_repository.py` (172줄)

커버 범위:
  - _row_to_record (JSON str/dict 파싱, defaults)
  - _row_to_verification (diffs None/list/str)
  - ExtractionRecordRepository.create / get_by_candidate / get_by_id
  - VerificationResultRepository.create / list_by_candidate
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from app.repositories.extraction_record_repository import (
    ExtractionRecordRepository,
    VerificationResultRepository,
    _row_to_record,
    _row_to_verification,
)
from app.models.extraction_record import (
    ExtractionRecord,
    MatchStatus,
    VerificationResult,
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


def _mk_record_row(result_as_str=False, scope_profile_id=None):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    result = {"title": "T"}
    if result_as_str:
        result = json.dumps(result)
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "extraction_candidate_id": "22222222-2222-2222-2222-222222222222",
        "document_id": "33333333-3333-3333-3333-333333333333",
        "document_version": 1,
        "document_content_hash": "abc123",
        "extraction_schema_id": "REPORT",
        "extraction_schema_version": 1,
        "extraction_model": "gpt-4o",
        "model_version": "v1",
        "extraction_prompt_version": "p1",
        "extraction_mode": "deterministic",
        "temperature": 0.0,
        "seed": 42,
        "extracted_result": result,
        "extracted_timestamp": now,
        "scope_profile_id": scope_profile_id,
        "actor_type": "agent",
        "created_at": now,
    }


def _mk_verification_row(diffs=None, diffs_as_str=False):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    if diffs is None:
        diffs = []
    if diffs_as_str:
        diffs = json.dumps(diffs)
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "extraction_candidate_id": "22222222-2222-2222-2222-222222222222",
        "verified_at": now,
        "match_status": "identical",
        "field_match_count": 10,
        "field_total_count": 10,
        "field_accuracy": 1.0,
        "diff_details": diffs,
        "error_message": None,
        "verified_by": "system",
        "actor_type": "user",
    }


# ---------------------------------------------------------------------------
# 1. _row_to_record
# ---------------------------------------------------------------------------


def test_row_to_record_dict_input():
    r = _row_to_record(_mk_record_row())
    assert r.extraction_model == "gpt-4o"
    assert r.extracted_result == {"title": "T"}


def test_row_to_record_json_string_input():
    r = _row_to_record(_mk_record_row(result_as_str=True))
    assert r.extracted_result == {"title": "T"}


def test_row_to_record_with_scope():
    r = _row_to_record(_mk_record_row(scope_profile_id=str(uuid4())))
    assert r.scope_profile_id is not None


def test_row_to_record_defaults_missing_optional_fields():
    row = _mk_record_row()
    del row["extraction_mode"]
    del row["extraction_model"]
    del row["actor_type"]
    r = _row_to_record(row)
    assert r.extraction_mode == "deterministic"
    assert r.extraction_model == "unknown"
    assert r.actor_type == "agent"


# ---------------------------------------------------------------------------
# 2. _row_to_verification
# ---------------------------------------------------------------------------


def test_row_to_verification_happy():
    v = _row_to_verification(_mk_verification_row())
    assert v.match_status == MatchStatus.IDENTICAL
    assert v.diff_details == []


def test_row_to_verification_with_diffs():
    diff = {
        "field_name": "title",
        "original_value": "A",
        "new_value": "B",
        "match_type": "mismatch",
    }
    v = _row_to_verification(_mk_verification_row(diffs=[diff]))
    assert len(v.diff_details) == 1


def test_row_to_verification_diffs_as_json_string():
    diff = {
        "field_name": "title",
        "original_value": "A",
        "new_value": "B",
        "match_type": "mismatch",
    }
    v = _row_to_verification(
        _mk_verification_row(diffs=[diff], diffs_as_str=True)
    )
    assert len(v.diff_details) == 1


# ---------------------------------------------------------------------------
# 3. ExtractionRecordRepository
# ---------------------------------------------------------------------------


def test_record_create_executes_insert():
    cur = _mk_cur(fetchone_values=[_mk_record_row()])
    repo = ExtractionRecordRepository(_mk_conn(cur))
    rec = ExtractionRecord(
        extraction_candidate_id=UUID("22222222-2222-2222-2222-222222222222"),
        document_id=UUID("33333333-3333-3333-3333-333333333333"),
        document_version=1,
        extraction_schema_id="REPORT",
        extraction_schema_version=1,
        extraction_model="gpt-4o",
        extraction_mode="deterministic",
        temperature=0.0,
        extracted_result={"title": "T"},
        extracted_timestamp=datetime.now(timezone.utc),
    )
    saved = repo.create(rec)
    assert saved.extraction_model == "gpt-4o"


def test_record_get_by_candidate_found():
    cur = _mk_cur(fetchone_values=[_mk_record_row()])
    repo = ExtractionRecordRepository(_mk_conn(cur))
    result = repo.get_by_candidate(
        UUID("22222222-2222-2222-2222-222222222222")
    )
    assert result is not None


def test_record_get_by_candidate_not_found():
    cur = _mk_cur(fetchone_values=[None])
    repo = ExtractionRecordRepository(_mk_conn(cur))
    assert repo.get_by_candidate(
        UUID("22222222-2222-2222-2222-222222222222")
    ) is None


def test_record_get_by_id_found():
    cur = _mk_cur(fetchone_values=[_mk_record_row()])
    repo = ExtractionRecordRepository(_mk_conn(cur))
    assert repo.get_by_id(
        UUID("11111111-1111-1111-1111-111111111111")
    ) is not None


def test_record_get_by_id_not_found():
    cur = _mk_cur(fetchone_values=[None])
    repo = ExtractionRecordRepository(_mk_conn(cur))
    assert repo.get_by_id(
        UUID("11111111-1111-1111-1111-111111111111")
    ) is None


# ---------------------------------------------------------------------------
# 4. VerificationResultRepository
# ---------------------------------------------------------------------------


def test_verification_create_executes_insert():
    cur = _mk_cur(fetchone_values=[_mk_verification_row()])
    repo = VerificationResultRepository(_mk_conn(cur))
    vr = VerificationResult(
        extraction_candidate_id=UUID("22222222-2222-2222-2222-222222222222"),
        verified_at=datetime.now(timezone.utc),
        match_status=MatchStatus.IDENTICAL,
        field_match_count=10,
        field_total_count=10,
        field_accuracy=1.0,
        diff_details=[],
    )
    saved = repo.create(vr)
    assert saved.match_status == MatchStatus.IDENTICAL


def test_verification_list_by_candidate():
    cur = _mk_cur(fetchall_values=[[_mk_verification_row(), _mk_verification_row()]])
    repo = VerificationResultRepository(_mk_conn(cur))
    result = repo.list_by_candidate(
        UUID("22222222-2222-2222-2222-222222222222")
    )
    assert len(result) == 2
