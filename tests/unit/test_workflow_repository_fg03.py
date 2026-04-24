"""
S3 Phase 0 / FG 0-3 후속 S2-A — `app.repositories.workflow_repository` 유닛 테스트.

본 파일은 실 DB 없이 psycopg2 cursor 를 mock 하여 SQL 파라미터 전달 + row 변환 헬퍼를
검증한다. 실 SQL 실행은 FG 0-1 통합 테스트 CI 가 담당.

커버 대상:
  - _row_to_review_action / _row_to_workflow_history / _row_to_change_log 3종
  - update_workflow_status / get_workflow_status
  - create_review_action / list_review_actions
  - create_workflow_history / list_workflow_history (version_id 필터 유무)
  - create_change_log
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterable
from unittest.mock import MagicMock

import pytest

from app.repositories.workflow_repository import (
    _row_to_change_log,
    _row_to_review_action,
    _row_to_workflow_history,
    workflow_repository,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Cursor mock 헬퍼
# --------------------------------------------------------------------------- #


def _make_conn(
    *,
    fetchone_values: list | None = None,
    fetchall_values: list | None = None,
):
    """conn.cursor() 가 순차적으로 fetchone/fetchall 결과를 반환하도록 세팅.

    multiple execute/fetchone 호출을 지원하기 위해 리스트로 전달.
    """
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

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn, cur


_NOW = datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc)


def _ra_row(**kw):
    base = {
        "id": "r-id-1", "document_id": "d1", "version_id": "v1",
        "action_type": "approve", "from_status": "in_review", "to_status": "approved",
        "actor_id": "u1", "actor_role": "APPROVER",
        "comment": None, "reason": None,
        "metadata": {"k": 1},
        "created_at": _NOW,
    }
    base.update(kw)
    return base


def _wh_row(**kw):
    base = {
        "id": "h-id-1", "document_id": "d1", "version_id": "v1",
        "from_status": "draft", "to_status": "in_review", "action": "submit_review",
        "actor_id": "u1", "actor_role": "AUTHOR",
        "comment": None, "reason": None,
        "created_at": _NOW,
    }
    base.update(kw)
    return base


def _cl_row(**kw):
    base = {
        "id": "c-id-1", "document_id": "d1", "version_id": "v1",
        "change_type": "workflow_transition.approve",
        "reason": None, "actor_id": "u1", "actor_role": "APPROVER",
        "metadata": None,
        "created_at": _NOW,
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# 1) Row 변환 헬퍼
# --------------------------------------------------------------------------- #


class TestRowConversion:
    def test_row_to_review_action(self):
        ra = _row_to_review_action(_ra_row())
        assert ra.id == "r-id-1"
        assert ra.action_type == "approve"
        assert ra.from_status == "in_review"
        assert ra.to_status == "approved"
        assert ra.metadata == {"k": 1}

    def test_row_to_review_action_metadata_defaults_to_empty(self):
        ra = _row_to_review_action(_ra_row(metadata=None))
        assert ra.metadata == {}

    def test_row_to_workflow_history(self):
        wh = _row_to_workflow_history(_wh_row())
        assert wh.id == "h-id-1"
        assert wh.action == "submit_review"

    def test_row_to_change_log_with_version(self):
        cl = _row_to_change_log(_cl_row(version_id="v-abc"))
        assert cl.id == "c-id-1"
        assert cl.version_id == "v-abc"
        assert cl.metadata == {}  # None → 빈 dict 기본

    def test_row_to_change_log_without_version_is_none(self):
        cl = _row_to_change_log(_cl_row(version_id=None))
        assert cl.version_id is None


# --------------------------------------------------------------------------- #
# 2) update_workflow_status / get_workflow_status
# --------------------------------------------------------------------------- #


class TestWorkflowStatusCRUD:
    def test_update_workflow_status_executes_update_sql(self):
        conn, cur = _make_conn()
        workflow_repository.update_workflow_status(conn, "v1", "in_review")
        assert cur.execute.called
        sql_arg = cur.execute.call_args.args[0]
        params = cur.execute.call_args.args[1]
        assert "UPDATE versions" in sql_arg
        assert "workflow_status = %s" in sql_arg
        assert params == ("in_review", "v1")

    def test_get_workflow_status_row_present(self):
        conn, _ = _make_conn(fetchone_values=[{"workflow_status": "approved"}])
        result = workflow_repository.get_workflow_status(conn, "v1")
        assert result == "approved"

    def test_get_workflow_status_row_absent(self):
        conn, _ = _make_conn(fetchone_values=[None])
        result = workflow_repository.get_workflow_status(conn, "missing")
        assert result is None


# --------------------------------------------------------------------------- #
# 3) review_actions
# --------------------------------------------------------------------------- #


class TestReviewActions:
    def test_create_review_action_inserts_and_returns_model(self):
        conn, cur = _make_conn(fetchone_values=[_ra_row(metadata={"admin": True})])
        ra = workflow_repository.create_review_action(
            conn, document_id="d1", version_id="v1",
            action_type="approve", from_status="in_review", to_status="approved",
            actor_id="u1", actor_role="APPROVER",
            metadata={"admin": True},
        )
        # 모델 변환 확인
        assert ra.id == "r-id-1"
        assert ra.metadata == {"admin": True}
        # INSERT SQL 호출 + 파라미터 마지막이 meta_json (직렬화된 JSON 문자열)
        params = cur.execute.call_args.args[1]
        assert len(params) == 10
        import json
        assert json.loads(params[-1]) == {"admin": True}

    def test_create_review_action_defaults_metadata_to_empty_json(self):
        conn, cur = _make_conn(fetchone_values=[_ra_row(metadata={})])
        workflow_repository.create_review_action(
            conn, document_id="d1", version_id="v1",
            action_type="submit_review", from_status="draft", to_status="in_review",
        )
        params = cur.execute.call_args.args[1]
        import json
        assert json.loads(params[-1]) == {}

    def test_list_review_actions_ordered_by_created_at(self):
        rows = [_ra_row(id="r1"), _ra_row(id="r2"), _ra_row(id="r3")]
        conn, cur = _make_conn(fetchall_values=[rows])
        result = workflow_repository.list_review_actions(conn, version_id="v1")
        assert len(result) == 3
        assert [r.id for r in result] == ["r1", "r2", "r3"]
        # ORDER BY created_at ASC 가 SQL 에 포함됨
        sql = cur.execute.call_args.args[0]
        assert "ORDER BY created_at ASC" in sql

    def test_list_review_actions_empty(self):
        conn, _ = _make_conn(fetchall_values=[[]])
        result = workflow_repository.list_review_actions(conn, version_id="v-none")
        assert result == []


# --------------------------------------------------------------------------- #
# 4) workflow_history
# --------------------------------------------------------------------------- #


class TestWorkflowHistory:
    def test_create_workflow_history_insert_and_return(self):
        conn, cur = _make_conn(fetchone_values=[_wh_row()])
        wh = workflow_repository.create_workflow_history(
            conn, document_id="d1", version_id="v1",
            from_status="draft", to_status="in_review", action="submit_review",
            actor_id="u1", actor_role="AUTHOR",
        )
        assert wh.id == "h-id-1"
        # INSERT 파라미터 개수 9 (version_id ~ reason)
        params = cur.execute.call_args.args[1]
        assert len(params) == 9

    def test_list_workflow_history_with_version_filter(self):
        # count + list 두 번의 fetchone / fetchall
        rows = [_wh_row(id="h1"), _wh_row(id="h2")]
        conn, cur = _make_conn(
            fetchone_values=[{"count": 2}],
            fetchall_values=[rows],
        )
        items, total = workflow_repository.list_workflow_history(
            conn, document_id="d1", version_id="v1", limit=50, offset=0,
        )
        assert total == 2
        assert [h.id for h in items] == ["h1", "h2"]
        # 두 번의 execute 호출 — count + list
        assert cur.execute.call_count == 2
        # 두 번째 SQL 에 version_id 조건이 포함됨
        list_sql = cur.execute.call_args_list[1].args[0]
        assert "AND version_id = %s" in list_sql
        assert "ORDER BY created_at DESC" in list_sql
        assert "LIMIT %s OFFSET %s" in list_sql

    def test_list_workflow_history_without_version_filter(self):
        conn, cur = _make_conn(
            fetchone_values=[{"count": 0}],
            fetchall_values=[[]],
        )
        items, total = workflow_repository.list_workflow_history(
            conn, document_id="d1", limit=10, offset=5,
        )
        assert items == []
        assert total == 0
        # 첫 SQL 에 version_id 조건 없어야 함
        count_sql = cur.execute.call_args_list[0].args[0]
        assert "AND version_id" not in count_sql


# --------------------------------------------------------------------------- #
# 5) change_logs
# --------------------------------------------------------------------------- #


class TestChangeLog:
    def test_create_change_log_metadata_serialized(self):
        conn, cur = _make_conn(fetchone_values=[_cl_row(metadata={"is_admin_override": True})])
        cl = workflow_repository.create_change_log(
            conn, document_id="d1", change_type="workflow_transition.approve",
            actor_id="u1", actor_role="ADMIN", version_id="v1",
            reason="emergency", metadata={"is_admin_override": True},
        )
        assert cl.id == "c-id-1"
        params = cur.execute.call_args.args[1]
        import json
        # metadata 는 마지막 파라미터
        assert json.loads(params[-1]) == {"is_admin_override": True}

    def test_create_change_log_with_version_id_none(self):
        conn, cur = _make_conn(fetchone_values=[_cl_row(version_id=None)])
        cl = workflow_repository.create_change_log(
            conn, document_id="d1", change_type="misc",
            version_id=None,
        )
        assert cl.version_id is None
