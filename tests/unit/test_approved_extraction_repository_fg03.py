"""FG 0-3 커버리지 보강 — approved_extraction_repository 유닛 테스트 (세션 13-A).

대상: `backend/app/repositories/approved_extraction_repository.py` (286줄)

커버 범위:
  - _row_to_model (dict/str JSON 파싱, None edits, tokens, scope)
  - create (기본, candidate_id None, tokens None → params)
  - get_by_id (found/None)
  - get_by_candidate (found/None)
  - list_by_document (기본, scope 필터)
  - list_recent (scope 없음/있음 → WHERE 분기)
  - soft_delete (True/False)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from app.repositories.approved_extraction_repository import (
    ApprovedExtractionRepository,
)
from app.models.approved_extraction import HumanEdit


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
    candidate_id="22222222-2222-2222-2222-222222222222",
    scope_profile_id=None,
    human_edits=None,
    approved_fields=None,
    tokens=None,
):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    return {
        "id": id_,
        "candidate_id": candidate_id,
        "document_id": "33333333-3333-3333-3333-333333333333",
        "document_version": 1,
        "extraction_schema_id": "REPORT",
        "extraction_schema_version": 1,
        "extraction_model": "gpt-4o",
        "extraction_latency_ms": 123,
        "extraction_tokens": tokens,
        "extraction_cost_estimate": None,
        "extraction_prompt_version": None,
        "approved_fields": approved_fields if approved_fields is not None else {"title": "T"},
        "human_edits": human_edits if human_edits is not None else [],
        "approved_by": "reviewer-1",
        "approved_at": now,
        "approval_comment": None,
        "actor_type": "user",
        "scope_profile_id": scope_profile_id,
        "created_at": now,
        "updated_at": now,
        "is_soft_deleted": False,
    }


# ---------------------------------------------------------------------------
# 1. _row_to_model
# ---------------------------------------------------------------------------


def test_row_to_model_happy_path():
    repo = ApprovedExtractionRepository(MagicMock())
    model = repo._row_to_model(_mk_row())
    assert model.extraction_model == "gpt-4o"
    assert model.actor_type == "user"


def test_row_to_model_parses_json_strings():
    repo = ApprovedExtractionRepository(MagicMock())
    row = _mk_row(
        human_edits=json.dumps([]),
        approved_fields=json.dumps({"title": "T"}),
        tokens=json.dumps({"input": 100}),
    )
    model = repo._row_to_model(row)
    assert model.approved_fields == {"title": "T"}


def test_row_to_model_null_candidate_id_is_none():
    repo = ApprovedExtractionRepository(MagicMock())
    row = _mk_row(candidate_id=None)
    model = repo._row_to_model(row)
    assert model.candidate_id is None


def test_row_to_model_with_scope():
    repo = ApprovedExtractionRepository(MagicMock())
    row = _mk_row(scope_profile_id=str(uuid4()))
    model = repo._row_to_model(row)
    assert model.scope_profile_id is not None


# ---------------------------------------------------------------------------
# 2. create
# ---------------------------------------------------------------------------


def test_create_basic():
    cur = _mk_cur(fetchone_values=[_mk_row()])
    repo = ApprovedExtractionRepository(_mk_conn(cur))
    result = repo.create(
        candidate_id=UUID("22222222-2222-2222-2222-222222222222"),
        document_id=UUID("33333333-3333-3333-3333-333333333333"),
        document_version=1,
        extraction_schema_id="REPORT",
        extraction_schema_version=1,
        extraction_model="gpt-4o",
        extraction_latency_ms=123,
        extraction_tokens=None,
        extraction_cost_estimate=None,
        extraction_prompt_version=None,
        approved_fields={"title": "T"},
        human_edits=[],
        approved_by="reviewer-1",
        approved_at=datetime(2026, 4, 24, tzinfo=timezone.utc),
        approval_comment=None,
    )
    assert result.approved_by == "reviewer-1"


def test_create_with_null_candidate_id():
    cur = _mk_cur(fetchone_values=[_mk_row(candidate_id=None)])
    repo = ApprovedExtractionRepository(_mk_conn(cur))
    repo.create(
        candidate_id=None,
        document_id=UUID("33333333-3333-3333-3333-333333333333"),
        document_version=1,
        extraction_schema_id="REPORT",
        extraction_schema_version=1,
        extraction_model="gpt-4o",
        extraction_latency_ms=0,
        extraction_tokens=None,
        extraction_cost_estimate=None,
        extraction_prompt_version=None,
        approved_fields={},
        human_edits=[],
        approved_by="u",
        approved_at=datetime.now(timezone.utc),
        approval_comment=None,
    )
    params = cur.execute.call_args[0][1]
    # candidate_id 위치에 None
    assert None in params


def test_create_with_tokens_and_scope():
    cur = _mk_cur(fetchone_values=[_mk_row()])
    repo = ApprovedExtractionRepository(_mk_conn(cur))
    sid = uuid4()
    repo.create(
        candidate_id=UUID("22222222-2222-2222-2222-222222222222"),
        document_id=UUID("33333333-3333-3333-3333-333333333333"),
        document_version=1,
        extraction_schema_id="REPORT",
        extraction_schema_version=1,
        extraction_model="gpt-4o",
        extraction_latency_ms=10,
        extraction_tokens={"input": 100, "output": 50},
        extraction_cost_estimate=0.01,
        extraction_prompt_version="v1",
        approved_fields={},
        human_edits=[],
        approved_by="u",
        approved_at=datetime.now(timezone.utc),
        approval_comment="ok",
        actor_type="agent",
        scope_profile_id=sid,
    )
    params = cur.execute.call_args[0][1]
    assert str(sid) in params
    assert "agent" in params


# ---------------------------------------------------------------------------
# 3. get_by_id / get_by_candidate
# ---------------------------------------------------------------------------


def test_get_by_id_found():
    cur = _mk_cur(fetchone_values=[_mk_row()])
    repo = ApprovedExtractionRepository(_mk_conn(cur))
    result = repo.get_by_id(UUID("11111111-1111-1111-1111-111111111111"))
    assert result is not None


def test_get_by_id_not_found():
    cur = _mk_cur(fetchone_values=[None])
    repo = ApprovedExtractionRepository(_mk_conn(cur))
    assert repo.get_by_id(UUID("11111111-1111-1111-1111-111111111111")) is None


def test_get_by_candidate_found():
    cur = _mk_cur(fetchone_values=[_mk_row()])
    repo = ApprovedExtractionRepository(_mk_conn(cur))
    result = repo.get_by_candidate(UUID("22222222-2222-2222-2222-222222222222"))
    assert result is not None


def test_get_by_candidate_not_found():
    cur = _mk_cur(fetchone_values=[None])
    repo = ApprovedExtractionRepository(_mk_conn(cur))
    assert repo.get_by_candidate(
        UUID("22222222-2222-2222-2222-222222222222")
    ) is None


# ---------------------------------------------------------------------------
# 4. list_by_document / list_recent
# ---------------------------------------------------------------------------


def test_list_by_document_basic():
    cur = _mk_cur(fetchall_values=[[_mk_row()]])
    repo = ApprovedExtractionRepository(_mk_conn(cur))
    result = repo.list_by_document(
        UUID("33333333-3333-3333-3333-333333333333")
    )
    assert len(result) == 1


def test_list_by_document_with_scope_filter():
    cur = _mk_cur(fetchall_values=[[]])
    repo = ApprovedExtractionRepository(_mk_conn(cur))
    sid = uuid4()
    repo.list_by_document(
        UUID("33333333-3333-3333-3333-333333333333"),
        scope_profile_id=sid,
    )
    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]
    assert "AND scope_profile_id = %s" in sql
    assert str(sid) in params


def test_list_recent_no_scope_uses_base_where():
    cur = _mk_cur(fetchall_values=[[_mk_row()]])
    repo = ApprovedExtractionRepository(_mk_conn(cur))
    repo.list_recent()
    sql = cur.execute.call_args[0][0]
    # scope 없으면 WHERE is_soft_deleted 만 — scope_profile_id 필터는 없어야 함
    # (단, SELECT 컬럼 목록에는 scope_profile_id 가 포함되므로 "=" 동반 여부로 검증)
    assert "WHERE is_soft_deleted = FALSE" in sql
    assert "scope_profile_id = %s" not in sql


def test_list_recent_with_scope_injects_filter():
    cur = _mk_cur(fetchall_values=[[]])
    repo = ApprovedExtractionRepository(_mk_conn(cur))
    sid = uuid4()
    repo.list_recent(scope_profile_id=sid)
    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]
    assert "scope_profile_id = %s" in sql
    assert str(sid) in params


# ---------------------------------------------------------------------------
# 5. soft_delete
# ---------------------------------------------------------------------------


def test_soft_delete_true_when_updated():
    cur = _mk_cur(rowcount=1)
    repo = ApprovedExtractionRepository(_mk_conn(cur))
    assert repo.soft_delete(
        UUID("11111111-1111-1111-1111-111111111111"), "admin-1"
    ) is True


def test_soft_delete_false_when_not_found():
    cur = _mk_cur(rowcount=0)
    repo = ApprovedExtractionRepository(_mk_conn(cur))
    assert repo.soft_delete(
        UUID("11111111-1111-1111-1111-111111111111"), "admin-1"
    ) is False
