"""FG 0-3 커버리지 보강 — agent_proposal_service 유닛 테스트 (세션 15).

대상: `backend/app/services/agent_proposal_service.py` (786줄)

커버 범위:
  - _assert_agent_active (found active / not found → NotFound / disabled → PermissionDenied)
  - _assert_document_exists (found / not found)
  - _create_document / _get_current_workflow_status (3분기) / _get_proposed_version
  - _record_acting_on_behalf_of (None / 정상 / 예외 swallow)
  - _create_mcp_task / _sync_mcp_task_state
  - propose_draft (새 문서 / 기존 문서 / document_type_id 누락 Conflict)
  - propose_transition (성공 / 버전 없음 → NotFound)
  - approve_draft (성공 / wrong state Conflict / bad role PermissionDenied)
  - reject_draft (성공 / wrong state / bad role)
  - withdraw_proposal (성공 / wrong state / 미발견)
  - batch_rollback (role 거부 / approve 롤백 / reject 롤백 / wrong status skip / committed skip)
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services import agent_proposal_service as aps_mod
from app.services.agent_proposal_service import AgentProposalService
from app.api.errors.exceptions import (
    ApiConflictError,
    ApiNotFoundError,
    ApiPermissionDeniedError,
)


# ---------------------------------------------------------------------------
# 공통 헬퍼 — 여러 execute 호출이 있으므로 복수 cursor 지원
# ---------------------------------------------------------------------------


class _CursorStub:
    """execute 호출 순서대로 fetchone/fetchall 값을 반환하는 stub.

    conn.cursor() 가 호출될 때마다 새 stub 을 반환하되, 모든 stub 이 같은
    fetchone_values / fetchall_values 큐를 공유하도록 한다.
    """

    def __init__(self, fetchone_queue, fetchall_queue):
        self._fetchone = fetchone_queue
        self._fetchall = fetchall_queue
        self.execute_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.execute_calls.append((sql, params))

    def fetchone(self):
        if self._fetchone:
            return self._fetchone.pop(0)
        return None

    def fetchall(self):
        if self._fetchall:
            return self._fetchall.pop(0)
        return []


def _mk_conn(fetchone_values=None, fetchall_values=None):
    """여러 번 cursor() 호출돼도 같은 큐를 소비하는 conn."""
    fo_q = list(fetchone_values or [])
    fa_q = list(fetchall_values or [])

    # 모든 cursor() 호출이 공유 큐를 소비하도록 단일 stub 재사용
    shared = _CursorStub(fo_q, fa_q)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=shared)
    return conn, shared


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """audit_emitter.emit 은 외부 호출 — 모든 테스트에서 mute."""
    monkeypatch.setattr(
        aps_mod.audit_emitter, "emit", MagicMock(return_value=None)
    )


@pytest.fixture(autouse=True)
def _mock_rebuild_nodes(monkeypatch):
    """Phase 1 FG 1-1: propose_draft 가 호출하는 snapshot_sync_service.rebuild_nodes_from_snapshot 은
    실 nodes_repository 를 호출해 _CursorStub.fetchone() = None → dict(None) 경로를 만든다.
    본 파일 mock conn 은 그 경로를 지원하지 않으므로 no-op 으로 교체한다.
    (rebuild 자체 검증은 test_snapshot_sync_service_fg11.py / test_agent_proposal_service_fg11.py 에서 수행)
    """
    import app.services.snapshot_sync_service as snap_mod
    monkeypatch.setattr(snap_mod, "rebuild_nodes_from_snapshot", lambda *a, **kw: [])


# ---------------------------------------------------------------------------
# 1. _assert_agent_active
# ---------------------------------------------------------------------------


def test_assert_agent_active_ok():
    svc = AgentProposalService()
    conn, _ = _mk_conn(fetchone_values=[{"id": "a1", "is_disabled": False}])
    svc._assert_agent_active(conn, "a1")  # no raise


def test_assert_agent_active_not_found():
    svc = AgentProposalService()
    conn, _ = _mk_conn(fetchone_values=[None])
    with pytest.raises(ApiNotFoundError):
        svc._assert_agent_active(conn, "a1")


def test_assert_agent_active_disabled_raises_permission():
    svc = AgentProposalService()
    conn, _ = _mk_conn(fetchone_values=[{"id": "a1", "is_disabled": True}])
    with pytest.raises(ApiPermissionDeniedError):
        svc._assert_agent_active(conn, "a1")


# ---------------------------------------------------------------------------
# 2. _assert_document_exists
# ---------------------------------------------------------------------------


def test_assert_document_exists_found():
    svc = AgentProposalService()
    conn, _ = _mk_conn(fetchone_values=[{"id": "d1"}])
    svc._assert_document_exists(conn, "d1")


def test_assert_document_exists_not_found():
    svc = AgentProposalService()
    conn, _ = _mk_conn(fetchone_values=[None])
    with pytest.raises(ApiNotFoundError):
        svc._assert_document_exists(conn, "d1")


# ---------------------------------------------------------------------------
# 3. _create_document
# ---------------------------------------------------------------------------


def test_create_document_returns_uuid():
    svc = AgentProposalService()
    conn, cur = _mk_conn()
    doc_id = svc._create_document(
        conn,
        title="T",
        document_type="REPORT",
        created_by="user-1",
        metadata={"source": "api"},
    )
    assert len(doc_id) == 36  # UUID 형식
    # INSERT 호출 확인
    assert cur.execute_calls


# ---------------------------------------------------------------------------
# 4. _get_current_workflow_status
# ---------------------------------------------------------------------------


def test_get_current_workflow_status_returns_workflow_status():
    svc = AgentProposalService()
    conn, _ = _mk_conn(
        fetchone_values=[{"workflow_status": "proposed", "status": "draft"}]
    )
    assert svc._get_current_workflow_status(conn, "d1") == "proposed"


def test_get_current_workflow_status_falls_back_to_status():
    svc = AgentProposalService()
    conn, _ = _mk_conn(
        fetchone_values=[{"workflow_status": None, "status": "approved"}]
    )
    assert svc._get_current_workflow_status(conn, "d1") == "approved"


def test_get_current_workflow_status_no_row_returns_draft():
    svc = AgentProposalService()
    conn, _ = _mk_conn(fetchone_values=[None])
    assert svc._get_current_workflow_status(conn, "d1") == "draft"


def test_get_current_workflow_status_both_none_returns_draft():
    svc = AgentProposalService()
    conn, _ = _mk_conn(
        fetchone_values=[{"workflow_status": None, "status": None}]
    )
    assert svc._get_current_workflow_status(conn, "d1") == "draft"


# ---------------------------------------------------------------------------
# 5. _get_proposed_version
# ---------------------------------------------------------------------------


def test_get_proposed_version_found():
    svc = AgentProposalService()
    conn, _ = _mk_conn(
        fetchone_values=[{
            "id": "v1", "document_id": "d1",
            "workflow_status": "proposed", "status": "draft",
        }]
    )
    version, doc_id = svc._get_proposed_version(conn, "v1")
    assert version["workflow_status"] == "proposed"
    assert doc_id == "d1"


def test_get_proposed_version_not_found():
    svc = AgentProposalService()
    conn, _ = _mk_conn(fetchone_values=[None])
    with pytest.raises(ApiNotFoundError):
        svc._get_proposed_version(conn, "v1")


# ---------------------------------------------------------------------------
# 6. _record_acting_on_behalf_of
# ---------------------------------------------------------------------------


def test_record_acting_on_behalf_of_none_skips():
    svc = AgentProposalService()
    conn = MagicMock()
    svc._record_acting_on_behalf_of(conn, agent_id="a1", acting_on_behalf_of=None)
    conn.cursor.assert_not_called()


def test_record_acting_on_behalf_of_executes_update():
    svc = AgentProposalService()
    conn, cur = _mk_conn()
    svc._record_acting_on_behalf_of(
        conn, agent_id="a1", acting_on_behalf_of="u1"
    )
    assert cur.execute_calls
    sql = cur.execute_calls[0][0]
    assert "UPDATE audit_events" in sql


def test_record_acting_on_behalf_of_swallows_exception():
    svc = AgentProposalService()
    conn = MagicMock()
    conn.cursor = MagicMock(side_effect=RuntimeError("db down"))
    # 예외가 전파되지 않아야 함
    svc._record_acting_on_behalf_of(
        conn, agent_id="a1", acting_on_behalf_of="u1"
    )


# ---------------------------------------------------------------------------
# 7. _create_mcp_task / _sync_mcp_task_state
# ---------------------------------------------------------------------------


def test_create_mcp_task_returns_uuid():
    svc = AgentProposalService()
    conn, cur = _mk_conn()
    task_id = svc._create_mcp_task(
        conn,
        title="Review task",
        description="desc",
        reference_type="version",
        reference_id="v1",
        agent_id="a1",
    )
    assert len(task_id) == 36


def test_create_mcp_task_truncates_long_title():
    svc = AgentProposalService()
    conn, cur = _mk_conn()
    long_title = "x" * 1000
    svc._create_mcp_task(
        conn,
        title=long_title,
        description=None,
        reference_type="version",
        reference_id="v1",
    )
    params = cur.execute_calls[0][1]
    # title 이 500자로 절단됨
    assert any(isinstance(p, str) and len(p) == 500 for p in params)


def test_sync_mcp_task_state_completed_sets_progress_100():
    svc = AgentProposalService()
    conn, cur = _mk_conn()
    svc._sync_mcp_task_state(
        conn,
        reference_type="version",
        reference_id="v1",
        new_state="completed",
    )
    params = cur.execute_calls[0][1]
    # progress=100 이 파라미터에 포함
    assert 100 in params


def test_sync_mcp_task_state_in_progress_sets_progress_50():
    svc = AgentProposalService()
    conn, cur = _mk_conn()
    svc._sync_mcp_task_state(
        conn,
        reference_type="version",
        reference_id="v1",
        new_state="in_progress",
    )
    params = cur.execute_calls[0][1]
    assert 50 in params


# ---------------------------------------------------------------------------
# 8. propose_draft
# ---------------------------------------------------------------------------


def test_propose_draft_new_document_requires_type_id():
    svc = AgentProposalService()
    conn, _ = _mk_conn(
        fetchone_values=[{"id": "a1", "is_disabled": False}]  # agent active
    )
    with pytest.raises(ApiConflictError):
        svc.propose_draft(
            conn,
            agent_id="a1",
            acting_on_behalf_of=None,
            document_id=None,
            document_type_id=None,  # 누락
            title="T",
            content="body",
            metadata={},
            reason="test",
        )


def test_propose_draft_new_document_success(monkeypatch):
    svc = AgentProposalService()
    monkeypatch.setattr(
        aps_mod.versions_repository, "get_next_version_number",
        lambda conn, doc_id: 1,
    )
    conn, _ = _mk_conn(
        fetchone_values=[
            {"id": "a1", "is_disabled": False},  # _assert_agent_active
        ]
    )
    result = svc.propose_draft(
        conn,
        agent_id="a1",
        acting_on_behalf_of="user-1",
        document_id=None,
        document_type_id="REPORT",
        title="새 문서",
        content="본문",
        metadata={"k": "v"},
        reason="사유",
    )
    assert result["status"] == "proposed"
    assert result["created_by_agent"] is True
    assert "mcp_task_id" in result


def test_propose_draft_existing_document_success(monkeypatch):
    svc = AgentProposalService()
    monkeypatch.setattr(
        aps_mod.versions_repository, "get_next_version_number",
        lambda conn, doc_id: 2,
    )
    conn, _ = _mk_conn(
        fetchone_values=[
            {"id": "a1", "is_disabled": False},  # agent active
            {"id": "d1"},  # document exists
        ]
    )
    result = svc.propose_draft(
        conn,
        agent_id="a1",
        acting_on_behalf_of=None,
        document_id="d1",
        document_type_id=None,
        title="수정",
        content="새 본문",
        metadata={},
        reason="",
    )
    assert result["document_id"] == "d1"
    assert result["status"] == "proposed"


# ---------------------------------------------------------------------------
# 9. propose_transition
# ---------------------------------------------------------------------------


def test_propose_transition_success():
    svc = AgentProposalService()
    conn, cur = _mk_conn(
        fetchone_values=[
            {"id": "a1", "is_disabled": False},  # agent
            {"id": "d1"},  # document
            {"workflow_status": "draft", "status": "draft"},  # current status
            {"id": "v99"},  # 최신 버전
        ]
    )
    result = svc.propose_transition(
        conn,
        agent_id="a1",
        acting_on_behalf_of=None,
        document_id="d1",
        target_state="published",
        reason="리뷰 완료",
        approver_notes=None,
    )
    assert result["proposed_state"] == "published"
    assert result["status"] == "pending_approval"


def test_propose_transition_no_version_raises_not_found():
    svc = AgentProposalService()
    conn, _ = _mk_conn(
        fetchone_values=[
            {"id": "a1", "is_disabled": False},  # agent
            {"id": "d1"},  # document
            {"workflow_status": "draft", "status": "draft"},  # current status
            None,  # 버전 없음
        ]
    )
    with pytest.raises(ApiNotFoundError):
        svc.propose_transition(
            conn,
            agent_id="a1",
            acting_on_behalf_of=None,
            document_id="d1",
            target_state="published",
            reason="r",
        )


# ---------------------------------------------------------------------------
# 10. approve_draft
# ---------------------------------------------------------------------------


def test_approve_draft_success():
    svc = AgentProposalService()
    conn, _ = _mk_conn(
        fetchone_values=[
            {"id": "v1", "document_id": "d1",
             "workflow_status": "proposed", "status": "draft"},
        ]
    )
    result = svc.approve_draft(
        conn,
        draft_id="v1",
        reviewer_id="rev-1",
        reviewer_role="REVIEWER",
        notes="검토 완료",
    )
    assert result["new_status"] == "approved"
    assert result["reviewed_by"] == "rev-1"


def test_approve_draft_wrong_state_raises_conflict():
    svc = AgentProposalService()
    conn, _ = _mk_conn(
        fetchone_values=[
            {"id": "v1", "document_id": "d1",
             "workflow_status": "approved", "status": "draft"},
        ]
    )
    with pytest.raises(ApiConflictError):
        svc.approve_draft(
            conn,
            draft_id="v1",
            reviewer_id="rev-1",
            reviewer_role="REVIEWER",
        )


def test_approve_draft_bad_role_raises_permission_denied():
    svc = AgentProposalService()
    conn, _ = _mk_conn(
        fetchone_values=[
            {"id": "v1", "document_id": "d1",
             "workflow_status": "proposed", "status": "draft"},
        ]
    )
    with pytest.raises(ApiPermissionDeniedError):
        svc.approve_draft(
            conn,
            draft_id="v1",
            reviewer_id="rev-1",
            reviewer_role="VIEWER",  # 권한 없음
        )


def test_approve_draft_not_found_raises():
    svc = AgentProposalService()
    conn, _ = _mk_conn(fetchone_values=[None])
    with pytest.raises(ApiNotFoundError):
        svc.approve_draft(
            conn, draft_id="v1", reviewer_id="rev-1", reviewer_role="REVIEWER"
        )


# ---------------------------------------------------------------------------
# 11. reject_draft
# ---------------------------------------------------------------------------


def test_reject_draft_success():
    svc = AgentProposalService()
    conn, _ = _mk_conn(
        fetchone_values=[
            {"id": "v1", "document_id": "d1",
             "workflow_status": "proposed", "status": "draft"},
        ]
    )
    result = svc.reject_draft(
        conn,
        draft_id="v1",
        reviewer_id="rev-1",
        reviewer_role="APPROVER",
        reason="부정확한 정보",
    )
    assert result["new_status"] == "rejected"
    assert result["reason"] == "부정확한 정보"


def test_reject_draft_wrong_state_raises_conflict():
    svc = AgentProposalService()
    conn, _ = _mk_conn(
        fetchone_values=[
            {"id": "v1", "document_id": "d1",
             "workflow_status": "withdrawn", "status": "draft"},
        ]
    )
    with pytest.raises(ApiConflictError):
        svc.reject_draft(
            conn, draft_id="v1", reviewer_id="r",
            reviewer_role="REVIEWER", reason="r",
        )


def test_reject_draft_bad_role_raises_permission():
    svc = AgentProposalService()
    conn, _ = _mk_conn(
        fetchone_values=[
            {"id": "v1", "document_id": "d1",
             "workflow_status": "proposed", "status": "draft"},
        ]
    )
    with pytest.raises(ApiPermissionDeniedError):
        svc.reject_draft(
            conn, draft_id="v1", reviewer_id="r",
            reviewer_role=None, reason="r",
        )


# ---------------------------------------------------------------------------
# 12. withdraw_proposal
# ---------------------------------------------------------------------------


def test_withdraw_proposal_success():
    svc = AgentProposalService()
    conn, _ = _mk_conn(
        fetchone_values=[
            {"id": "a1", "is_disabled": False},  # agent active
            {"id": "v1", "document_id": "d1",
             "workflow_status": "proposed", "created_by": "a1"},
        ]
    )
    result = svc.withdraw_proposal(
        conn, agent_id="a1", proposal_id="v1", reason="더 개선"
    )
    assert result["new_status"] == "withdrawn"


def test_withdraw_proposal_not_found_raises():
    svc = AgentProposalService()
    conn, _ = _mk_conn(
        fetchone_values=[
            {"id": "a1", "is_disabled": False},  # agent active
            None,  # version 조회 None
        ]
    )
    with pytest.raises(ApiNotFoundError):
        svc.withdraw_proposal(conn, agent_id="a1", proposal_id="v1")


def test_withdraw_proposal_wrong_state_raises_conflict():
    svc = AgentProposalService()
    conn, _ = _mk_conn(
        fetchone_values=[
            {"id": "a1", "is_disabled": False},
            {"id": "v1", "document_id": "d1",
             "workflow_status": "approved", "created_by": "a1"},
        ]
    )
    with pytest.raises(ApiConflictError):
        svc.withdraw_proposal(conn, agent_id="a1", proposal_id="v1")


# ---------------------------------------------------------------------------
# 13. batch_rollback
# ---------------------------------------------------------------------------


def test_batch_rollback_bad_role_raises_permission():
    svc = AgentProposalService()
    with pytest.raises(ApiPermissionDeniedError):
        svc.batch_rollback(
            MagicMock(),
            proposal_ids=["p1"],
            original_action="approve",
            reviewer_id="u",
            reviewer_role="VIEWER",
        )


def test_batch_rollback_approve_success():
    svc = AgentProposalService()
    conn, _ = _mk_conn(
        fetchone_values=[
            {"id": "p1", "status": "approved",
             "reference_id": "v1", "proposal_type": "draft",
             "workflow_status": "approved"},
        ]
    )
    result = svc.batch_rollback(
        conn,
        proposal_ids=["p1"],
        original_action="approve",
        reviewer_id="admin",
        reviewer_role="ORG_ADMIN",
    )
    assert result["rolled_back"] == 1
    assert result["skipped"] == 0


def test_batch_rollback_reject_success():
    svc = AgentProposalService()
    conn, _ = _mk_conn(
        fetchone_values=[
            {"id": "p1", "status": "rejected",
             "reference_id": "v1", "proposal_type": "draft",
             "workflow_status": "rejected"},
        ]
    )
    result = svc.batch_rollback(
        conn,
        proposal_ids=["p1"],
        original_action="reject",
        reviewer_id="admin",
        reviewer_role="APPROVER",
    )
    assert result["rolled_back"] == 1


def test_batch_rollback_skipped_when_status_mismatch():
    svc = AgentProposalService()
    conn, _ = _mk_conn(
        fetchone_values=[
            {"id": "p1", "status": "pending",  # 잘못된 상태
             "reference_id": "v1", "proposal_type": "draft",
             "workflow_status": "proposed"},
        ]
    )
    result = svc.batch_rollback(
        conn,
        proposal_ids=["p1"],
        original_action="approve",  # 'approved' 기대
        reviewer_id="admin",
        reviewer_role="ORG_ADMIN",
    )
    assert result["rolled_back"] == 0
    assert result["skipped"] == 1
    assert "p1" in result["skipped_ids"]


def test_batch_rollback_skipped_when_committed():
    svc = AgentProposalService()
    conn, _ = _mk_conn(
        fetchone_values=[
            {"id": "p1", "status": "approved",
             "reference_id": "v1", "proposal_type": "draft",
             "workflow_status": "committed"},  # 이미 commit 된 버전
        ]
    )
    result = svc.batch_rollback(
        conn,
        proposal_ids=["p1"],
        original_action="approve",
        reviewer_id="admin",
        reviewer_role="ORG_ADMIN",
    )
    assert result["rolled_back"] == 0
    assert result["skipped"] == 1


def test_batch_rollback_skipped_when_proposal_missing():
    svc = AgentProposalService()
    conn, _ = _mk_conn(fetchone_values=[None])  # SELECT → None
    result = svc.batch_rollback(
        conn,
        proposal_ids=["p1"],
        original_action="approve",
        reviewer_id="admin",
        reviewer_role="ORG_ADMIN",
    )
    assert result["skipped"] == 1


def test_batch_rollback_multiple_proposals_mixed():
    svc = AgentProposalService()
    conn, _ = _mk_conn(
        fetchone_values=[
            {"id": "p1", "status": "approved",
             "reference_id": "v1", "proposal_type": "draft",
             "workflow_status": "approved"},
            {"id": "p2", "status": "pending",  # skip
             "reference_id": "v2", "proposal_type": "draft",
             "workflow_status": "proposed"},
            {"id": "p3", "status": "approved",
             "reference_id": "v3", "proposal_type": "transition",
             "workflow_status": None},  # transition — version update 미수행
        ]
    )
    result = svc.batch_rollback(
        conn,
        proposal_ids=["p1", "p2", "p3"],
        original_action="approve",
        reviewer_id="admin",
        reviewer_role="SUPER_ADMIN",
    )
    assert result["rolled_back"] == 2  # p1, p3
    assert result["skipped"] == 1


# ---------------------------------------------------------------------------
# 14. 싱글턴 존재
# ---------------------------------------------------------------------------


def test_singleton_exists():
    assert isinstance(aps_mod.agent_proposal_service, AgentProposalService)
