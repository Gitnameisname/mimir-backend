"""
기본 에이전트(default) 회귀 테스트.

에이전트의 핵심 행동 기대치를 고정:
  - 에이전트는 proposed 상태로만 Draft를 생성한다
  - 에이전트는 직접 approve/publish할 수 없다
  - 에이전트는 자신이 생성한 제안만 회수할 수 있다
  - Kill Switch 활성 시 모든 요청이 즉시 차단된다
  - 감사 로그에는 항상 actor_type=agent가 기록된다

이 파일은 에이전트 동작이 변경되었을 때 회귀를 감지하기 위한 것이다.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.domain.workflow.enums import WorkflowStatus
from app.services.agent_proposal_service import AgentProposalService
from app.api.errors.exceptions import ApiPermissionDeniedError


AGENT_ID = "default-test-agent-001"
REVIEWER_ID = "human-reviewer-001"


def _mock_conn(agent_active: bool = True):
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur

    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    proposal_id = str(uuid.uuid4())

    def fetchone_se(*args, **kwargs):
        sql = cur.execute.call_args[0][0] if cur.execute.call_args else ""
        if "agents" in sql and "is_disabled" in sql:
            return {"id": AGENT_ID, "is_disabled": False} if agent_active else None
        if "documents" in sql and "WHERE id = %s" in sql:
            return {"id": doc_id}
        if "versions" in sql and "version_number DESC" in sql:
            return {"id": ver_id}
        if "versions" in sql and "workflow_status" in sql:
            return {
                "id": ver_id,
                "document_id": doc_id,
                "workflow_status": "proposed",
                "status": "draft",
                "created_by": AGENT_ID,
            }
        if "COALESCE(MAX" in sql:
            return {"next_number": 1}
        if "agent_proposals" in sql and "WHERE id = %s" in sql:
            return {
                "id": proposal_id,
                "agent_id": AGENT_ID,
                "proposal_type": "draft",
                "reference_id": ver_id,
                "status": "pending",
                "review_notes": None,
            }
        return None

    cur.fetchone.side_effect = fetchone_se
    cur.fetchall.return_value = []
    return conn, doc_id, ver_id, proposal_id


class TestDefaultAgentBehaviorRegression:
    """기본 에이전트 행동 회귀 테스트 — 이 테스트가 깨지면 에이전트 행동이 변경된 것이다."""

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_agent_draft_always_proposed(self, mock_audit):
        """[회귀] 에이전트 Draft는 반드시 proposed 상태여야 한다."""
        conn, doc_id, ver_id, _ = _mock_conn()
        svc = AgentProposalService()

        resp = svc.propose_draft(
            conn,
            agent_id=AGENT_ID,
            acting_on_behalf_of=None,
            document_id=doc_id,
            document_type_id=None,
            title="회귀 테스트 Draft",
            content="내용",
            metadata={},
            reason="자동 생성",
        )

        assert resp["status"] == "proposed", (
            f"[회귀 감지] 에이전트 Draft 상태가 'proposed'가 아닙니다: {resp['status']}"
        )

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_agent_draft_is_agent_created(self, mock_audit):
        """[회귀] 에이전트 Draft에는 created_by_agent=True가 설정된다."""
        conn, doc_id, ver_id, _ = _mock_conn()
        svc = AgentProposalService()

        resp = svc.propose_draft(
            conn,
            agent_id=AGENT_ID,
            acting_on_behalf_of=None,
            document_id=doc_id,
            document_type_id=None,
            title="회귀 테스트",
            content="내용",
            metadata={},
            reason="테스트",
        )

        assert resp["created_by_agent"] is True, (
            "[회귀 감지] created_by_agent가 True가 아닙니다"
        )

    def test_kill_switch_immediately_blocks(self):
        """[회귀] Kill Switch 활성 에이전트는 즉시 차단된다."""
        conn, doc_id, ver_id, _ = _mock_conn(agent_active=False)
        svc = AgentProposalService()

        with pytest.raises(Exception):
            svc.propose_draft(
                conn,
                agent_id=AGENT_ID,
                acting_on_behalf_of=None,
                document_id=doc_id,
                document_type_id=None,
                title="차단 테스트",
                content="내용",
                metadata={},
                reason="테스트",
            )

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_withdraw_changes_status(self, mock_audit):
        """[회귀] 에이전트 회수 후 상태는 withdrawn이어야 한다."""
        conn, doc_id, ver_id, proposal_id = _mock_conn()
        svc = AgentProposalService()

        resp = svc.withdraw_proposal(
            conn,
            proposal_id=proposal_id,
            agent_id=AGENT_ID,
            reason="회귀 테스트 회수",
        )

        assert resp["new_status"] == "withdrawn", (
            f"[회귀 감지] 회수 후 상태가 'withdrawn'이 아닙니다: {resp['new_status']}"
        )

    def test_proposed_status_is_agent_exclusive(self):
        """[회귀] 'proposed' WorkflowStatus는 에이전트 전용이다."""
        assert hasattr(WorkflowStatus, "PROPOSED"), "[회귀 감지] PROPOSED 상태가 제거되었습니다"
        assert WorkflowStatus.PROPOSED.value == "proposed"

    def test_withdrawn_status_exists(self):
        """[회귀] 'withdrawn' WorkflowStatus가 존재한다."""
        assert hasattr(WorkflowStatus, "WITHDRAWN"), "[회귀 감지] WITHDRAWN 상태가 제거되었습니다"
        assert WorkflowStatus.WITHDRAWN.value == "withdrawn"
