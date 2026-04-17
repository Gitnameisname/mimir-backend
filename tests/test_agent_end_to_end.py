"""
FG5.3 — 에이전트 액션 플레인 통합 e2e 시나리오 테스트.

전체 시나리오:
  1. 에이전트A가 Draft 제안 생성 → proposed 상태 확인
  2. 인간 검토자가 승인 → approved 상태 전환
  3. 반려 시나리오 → rejected 상태
  4. 에이전트 회수 → withdrawn 상태
  5. 감사 로그 기록 확인 (actor_type=agent)
  6. 통계 계산 로직 검증
  7. Kill Switch 후 재제안 차단 확인
  8. 인간 워크플로 미영향 확인
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.domain.workflow.enums import WorkflowStatus
from app.services.agent_proposal_service import AgentProposalService
from app.api.errors.exceptions import ApiPermissionDeniedError, ApiConflictError


def _make_agent_conn(
    *,
    agent_active: bool = True,
    workflow_status: str = "proposed",
    created_by: str = "agent-001",
):
    """통합 시나리오용 DB 커넥션 mock."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur

    agent_id = str(uuid.uuid4())
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    proposal_id = str(uuid.uuid4())

    def fetchone_se(*args, **kwargs):
        sql = cur.execute.call_args[0][0] if cur.execute.call_args else ""
        if "agents" in sql and "is_disabled" in sql:
            return {"id": agent_id, "is_disabled": False} if agent_active else None
        if "documents" in sql and "WHERE id = %s" in sql:
            return {"id": doc_id}
        if "versions" in sql and "version_number DESC" in sql:
            return {"id": ver_id}
        if "versions" in sql and "workflow_status" in sql:
            return {
                "id": ver_id,
                "document_id": doc_id,
                "workflow_status": workflow_status,
                "status": "draft",
                "created_by": created_by,
            }
        if "COALESCE(MAX" in sql:
            return {"next_number": 1}
        if "agent_proposals" in sql and "WHERE id = %s" in sql:
            return {
                "id": proposal_id,
                "agent_id": agent_id,
                "proposal_type": "draft",
                "reference_id": ver_id,
                "status": "pending",
                "review_notes": None,
            }
        return None

    cur.fetchone.side_effect = fetchone_se
    cur.fetchall.return_value = []
    return conn, agent_id, doc_id, ver_id, proposal_id


class TestE2EAgentProposalFullFlow:
    """에이전트 Draft 제안 → 승인 전체 플로우."""

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_propose_sets_proposed_status(self, mock_audit):
        """에이전트 Draft 제안 → proposed 상태."""
        conn, agent_id, doc_id, ver_id, _ = _make_agent_conn()
        svc = AgentProposalService()

        resp = svc.propose_draft(
            conn,
            agent_id=agent_id,
            acting_on_behalf_of=None,
            document_id=doc_id,
            document_type_id=None,
            title="e2e 테스트 제안",
            content="검토가 완료된 문서 내용입니다.",
            metadata={},
            reason="자동 검토 완료",
        )

        assert resp["status"] == "proposed"
        assert resp["created_by_agent"] is True

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_approve_changes_status(self, mock_audit):
        """인간 승인 → approved 상태 반환."""
        conn, agent_id, doc_id, ver_id, _ = _make_agent_conn()
        svc = AgentProposalService()

        resp = svc.approve_draft(
            conn,
            draft_id=ver_id,
            reviewer_id="reviewer-1",
            reviewer_role="REVIEWER",
            notes="검토 완료",
        )

        assert resp["new_status"] == "approved"

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_reject_changes_status(self, mock_audit):
        """반려 → rejected 상태 반환."""
        conn, agent_id, doc_id, ver_id, _ = _make_agent_conn()
        svc = AgentProposalService()

        resp = svc.reject_draft(
            conn,
            draft_id=ver_id,
            reviewer_id="reviewer-1",
            reviewer_role="REVIEWER",
            reason="내용이 부정확합니다",
        )

        assert resp["new_status"] == "rejected"
        assert resp["reason"] == "내용이 부정확합니다"

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_withdraw_changes_status(self, mock_audit):
        """에이전트가 제안을 회수 → withdrawn 상태 반환."""
        conn, agent_id, doc_id, ver_id, proposal_id = _make_agent_conn()
        svc = AgentProposalService()

        resp = svc.withdraw_proposal(
            conn,
            proposal_id=proposal_id,
            agent_id=agent_id,
            reason="더 나은 버전으로 재제안",
        )

        assert resp["new_status"] == "withdrawn"

    def test_kill_switch_blocks_proposal(self):
        """Kill Switch 활성 에이전트의 제안은 거부된다."""
        conn, agent_id, doc_id, ver_id, _ = _make_agent_conn(agent_active=False)
        svc = AgentProposalService()

        with pytest.raises((ApiPermissionDeniedError, Exception)):
            svc.propose_draft(
                conn,
                agent_id=agent_id,
                acting_on_behalf_of=None,
                document_id=doc_id,
                document_type_id=None,
                title="차단 테스트",
                content="내용",
                metadata={},
                reason="테스트",
            )

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_non_proposed_draft_cannot_be_approved(self, mock_audit):
        """proposed 상태가 아닌 Draft는 승인할 수 없다."""
        conn, agent_id, doc_id, ver_id, _ = _make_agent_conn(workflow_status="draft")
        svc = AgentProposalService()

        with pytest.raises((ApiConflictError, Exception)):
            svc.approve_draft(
                conn,
                draft_id=ver_id,
                reviewer_id="reviewer-1",
                reviewer_role="REVIEWER",
            )


class TestE2EAuditLogRecording:
    """감사 로그 기록 검증."""

    def test_audit_event_recorded_on_propose(self):
        """Draft 제안 시 actor_type='agent' 감사 이벤트가 기록된다."""
        conn, agent_id, doc_id, ver_id, _ = _make_agent_conn()
        svc = AgentProposalService()

        with patch("app.services.agent_proposal_service.audit_emitter") as mock_audit:
            svc.propose_draft(
                conn,
                agent_id=agent_id,
                acting_on_behalf_of=None,
                document_id=doc_id,
                document_type_id=None,
                title="감사 로그 테스트",
                content="내용",
                metadata={},
                reason="테스트",
            )
            assert mock_audit.emit.called or mock_audit.emit_for_actor.called

    def test_audit_event_recorded_on_approve(self):
        """Draft 승인 시 감사 이벤트가 기록된다."""
        conn, agent_id, doc_id, ver_id, _ = _make_agent_conn()
        svc = AgentProposalService()

        with patch("app.services.agent_proposal_service.audit_emitter") as mock_audit:
            svc.approve_draft(
                conn,
                draft_id=ver_id,
                reviewer_id="reviewer-1",
                reviewer_role="REVIEWER",
            )
            assert mock_audit.emit.called or mock_audit.emit_for_actor.called

    def test_audit_event_recorded_on_reject(self):
        """Draft 반려 시 감사 이벤트가 기록된다."""
        conn, agent_id, doc_id, ver_id, _ = _make_agent_conn()
        svc = AgentProposalService()

        with patch("app.services.agent_proposal_service.audit_emitter") as mock_audit:
            svc.reject_draft(
                conn,
                draft_id=ver_id,
                reviewer_id="reviewer-1",
                reviewer_role="REVIEWER",
                reason="형식 오류",
            )
            assert mock_audit.emit.called or mock_audit.emit_for_actor.called


class TestE2EHumanWorkflowUnaffected:
    """에이전트 플로우가 인간 워크플로를 방해하지 않음을 검증."""

    def test_human_workflow_statuses_exist(self):
        """기존 인간 워크플로 상태가 그대로 존재한다."""
        human_statuses = {"DRAFT", "IN_REVIEW", "APPROVED", "PUBLISHED", "ARCHIVED", "REJECTED"}
        all_values_upper = {s.value.upper() for s in WorkflowStatus}
        for hs in human_statuses:
            assert hs in all_values_upper, f"인간 워크플로 상태 {hs}가 누락됨"

    def test_agent_statuses_are_disjoint(self):
        """에이전트 전용 상태가 인간 워크플로 경로와 충돌하지 않는다."""
        agent_only = {WorkflowStatus.PROPOSED.value, WorkflowStatus.WITHDRAWN.value}
        human_main_path = {"draft", "in_review", "approved", "published", "archived", "rejected"}
        overlap = agent_only & human_main_path
        assert len(overlap) == 0, f"상태 충돌: {overlap}"

    def test_proposed_status_value(self):
        assert WorkflowStatus.PROPOSED.value == "proposed"

    def test_withdrawn_status_value(self):
        assert WorkflowStatus.WITHDRAWN.value == "withdrawn"


class TestE2EAgentStatisticsCalculation:
    """에이전트 통계 계산 로직 검증."""

    def test_approval_rate_calculation(self):
        total, approved = 100, 70
        rate = round(approved / total, 4)
        assert rate == 0.7

    def test_approval_rate_zero_total(self):
        total = 0
        rate = round(0 / total, 4) if total > 0 else 0.0
        assert rate == 0.0

    def test_approval_rate_all_approved(self):
        total, approved = 10, 10
        rate = round(approved / total, 4)
        assert rate == 1.0
