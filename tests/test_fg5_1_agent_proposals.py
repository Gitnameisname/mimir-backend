"""
FG5.1 단위 테스트 — 에이전트 Draft 제안 / 워크플로 전이 제안.

검수 기준:
  ✅ 에이전트가 생성한 Draft의 status가 반드시 'proposed'인가
  ✅ 인간 Draft 경로(draft → in_review → approved → published)는 여전히 사용 가능한가
  ✅ Draft 승인 시 status가 'approved'로 정확히 전환되는가
  ✅ 감사 로그에 에이전트 Draft 생성/승인/거부 이벤트가 기록되는가
  ✅ MCP Tasks 상태 변화가 Draft 상태와 동기화되는가
  ✅ 에이전트가 비활성화(Kill Switch) 상태이면 모든 제안 요청이 거부되는가
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, call, patch

import pytest

from app.api.errors.exceptions import (
    ApiConflictError,
    ApiNotFoundError,
    ApiPermissionDeniedError,
)
from app.domain.workflow.enums import WorkflowStatus
from app.services.agent_proposal_service import AgentProposalService

# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def _make_agent_id() -> str:
    return str(uuid.uuid4())


def _make_doc_id() -> str:
    return str(uuid.uuid4())


def _make_version_id() -> str:
    return str(uuid.uuid4())


def _make_conn_mock(
    *,
    agent_active: bool = True,
    document_exists: bool = True,
    version_workflow_status: str = "proposed",
    version_created_by: str = "agent-001",
) -> MagicMock:
    """DB 연결 mock을 구성한다."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor

    agent_id_val = _make_agent_id()
    doc_id_val = _make_doc_id()
    version_id_val = _make_version_id()

    def fetchone_side_effect(*args, **kwargs):
        sql = cursor.execute.call_args[0][0] if cursor.execute.call_args else ""
        if "agents" in sql and "is_disabled" in sql:
            if not agent_active:
                return None
            return {"id": agent_id_val, "is_disabled": False}
        if "documents" in sql and "WHERE id = %s" in sql:
            return {"id": doc_id_val} if document_exists else None
        if "versions" in sql and "version_number DESC" in sql:
            return {"id": version_id_val}
        if "versions" in sql and "workflow_status" in sql:
            return {
                "id": version_id_val,
                "document_id": doc_id_val,
                "workflow_status": version_workflow_status,
                "status": "draft",
                "created_by": version_created_by,
            }
        if "COALESCE(MAX" in sql:
            return {"next_number": 1}
        return None

    cursor.fetchone.side_effect = fetchone_side_effect
    return conn


# ---------------------------------------------------------------------------
# WorkflowStatus enum 확인
# ---------------------------------------------------------------------------

class TestWorkflowStatusEnum:
    def test_proposed_in_enum(self):
        assert WorkflowStatus.PROPOSED.value == "proposed"

    def test_withdrawn_in_enum(self):
        assert WorkflowStatus.WITHDRAWN.value == "withdrawn"

    def test_existing_statuses_unchanged(self):
        assert WorkflowStatus.DRAFT.value == "draft"
        assert WorkflowStatus.APPROVED.value == "approved"
        assert WorkflowStatus.PUBLISHED.value == "published"
        assert WorkflowStatus.REJECTED.value == "rejected"


# ---------------------------------------------------------------------------
# propose_draft 테스트
# ---------------------------------------------------------------------------

class TestProposeDraft:
    def setup_method(self):
        self.service = AgentProposalService()

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_propose_draft_sets_proposed_status(self, mock_audit):
        """에이전트가 생성한 Draft는 반드시 workflow_status = 'proposed'여야 한다."""
        agent_id = _make_agent_id()
        doc_id = _make_doc_id()
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        def fetchone_se(*args, **kwargs):
            sql = cursor.execute.call_args[0][0] if cursor.execute.call_args else ""
            if "agents" in sql and "is_disabled" in sql:
                return {"id": agent_id, "is_disabled": False}
            if "documents" in sql:
                return {"id": doc_id}
            if "COALESCE(MAX" in sql:
                return {"next_number": 1}
            return None

        cursor.fetchone.side_effect = fetchone_se

        result = self.service.propose_draft(
            conn,
            agent_id=agent_id,
            acting_on_behalf_of=None,
            document_id=doc_id,
            document_type_id=None,
            title="테스트 제안",
            content="본문 내용",
            metadata={},
            reason="자동 검토 완료",
        )

        assert result["status"] == "proposed"
        assert result["created_by_agent"] is True
        assert result["document_id"] == doc_id

        # INSERT에 'proposed'가 포함되어 있는지 확인
        insert_calls = [
            str(c) for c in cursor.execute.call_args_list if "INSERT INTO versions" in str(c)
        ]
        assert len(insert_calls) == 1
        assert "'proposed'" in insert_calls[0]

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_propose_draft_emits_audit_event(self, mock_audit):
        """감사 로그에 actor_type=agent, event_type=agent.draft.proposed가 기록되어야 한다."""
        agent_id = _make_agent_id()
        doc_id = _make_doc_id()
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        def fetchone_se(*args, **kwargs):
            sql = cursor.execute.call_args[0][0] if cursor.execute.call_args else ""
            if "agents" in sql and "is_disabled" in sql:
                return {"id": agent_id, "is_disabled": False}
            if "documents" in sql:
                return {"id": doc_id}
            if "COALESCE(MAX" in sql:
                return {"next_number": 1}
            return None

        cursor.fetchone.side_effect = fetchone_se

        self.service.propose_draft(
            conn,
            agent_id=agent_id,
            acting_on_behalf_of="user-123",
            document_id=doc_id,
            document_type_id=None,
            title="테스트",
            content="내용",
            metadata={},
            reason="검토 완료",
        )

        mock_audit.emit.assert_called_once()
        kwargs = mock_audit.emit.call_args[1]
        assert kwargs["event_type"] == "agent.draft.proposed"
        assert kwargs["actor_type"] == "agent"
        assert kwargs["actor_id"] == agent_id
        assert kwargs["new_state"] == "proposed"

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_propose_draft_disabled_agent_rejected(self, mock_audit):
        """Kill Switch 활성화된 에이전트는 제안을 생성할 수 없다."""
        agent_id = _make_agent_id()
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        cursor.fetchone.return_value = {"id": agent_id, "is_disabled": True}

        with pytest.raises(ApiPermissionDeniedError, match="Kill Switch"):
            self.service.propose_draft(
                conn,
                agent_id=agent_id,
                acting_on_behalf_of=None,
                document_id=_make_doc_id(),
                document_type_id=None,
                title=None,
                content="내용",
                metadata={},
                reason="이유",
            )

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_propose_draft_agent_not_found(self, mock_audit):
        """존재하지 않는 에이전트 ID로 제안하면 404를 반환해야 한다."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        cursor.fetchone.return_value = None

        with pytest.raises(ApiNotFoundError):
            self.service.propose_draft(
                conn,
                agent_id="non-existent-agent",
                acting_on_behalf_of=None,
                document_id=_make_doc_id(),
                document_type_id=None,
                title=None,
                content="내용",
                metadata={},
                reason="이유",
            )


# ---------------------------------------------------------------------------
# approve_draft 테스트
# ---------------------------------------------------------------------------

class TestApproveDraft:
    def setup_method(self):
        self.service = AgentProposalService()

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_approve_proposed_draft_to_approved(self, mock_audit):
        """승인 시 Draft의 workflow_status가 approved로 전환되어야 한다."""
        draft_id = _make_version_id()
        doc_id = _make_doc_id()

        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        cursor.fetchone.return_value = {
            "id": draft_id,
            "document_id": doc_id,
            "workflow_status": "proposed",
            "status": "draft",
        }

        result = self.service.approve_draft(
            conn,
            draft_id=draft_id,
            reviewer_id="reviewer-001",
            reviewer_role="APPROVER",
            notes="내용 확인 완료",
        )

        assert result["new_status"] == "approved"
        assert result["previous_status"] == "proposed"
        assert result["reviewed_by"] == "reviewer-001"

        # UPDATE 쿼리에 approved가 포함되어 있는지 확인
        update_calls = [
            str(c) for c in cursor.execute.call_args_list if "UPDATE versions" in str(c)
        ]
        assert any("approved" in c for c in update_calls)

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_approve_emits_audit_event(self, mock_audit):
        """승인 시 감사 로그가 actor_type=user로 기록되어야 한다."""
        draft_id = _make_version_id()
        doc_id = _make_doc_id()

        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        cursor.fetchone.return_value = {
            "id": draft_id,
            "document_id": doc_id,
            "workflow_status": "proposed",
            "status": "draft",
        }

        self.service.approve_draft(
            conn,
            draft_id=draft_id,
            reviewer_id="reviewer-001",
            reviewer_role="REVIEWER",
        )

        mock_audit.emit.assert_called_once()
        kwargs = mock_audit.emit.call_args[1]
        assert kwargs["event_type"] == "agent.draft.approved"
        assert kwargs["actor_type"] == "user"
        assert kwargs["new_state"] == "approved"

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_approve_non_proposed_status_raises_conflict(self, mock_audit):
        """proposed 상태가 아닌 Draft는 승인할 수 없다."""
        draft_id = _make_version_id()
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        cursor.fetchone.return_value = {
            "id": draft_id,
            "document_id": _make_doc_id(),
            "workflow_status": "draft",
            "status": "draft",
        }

        with pytest.raises(ApiConflictError, match="proposed"):
            self.service.approve_draft(
                conn,
                draft_id=draft_id,
                reviewer_id="reviewer-001",
                reviewer_role="APPROVER",
            )

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_approve_insufficient_role_raises_permission_denied(self, mock_audit):
        """AUTHOR 역할로는 승인할 수 없다."""
        draft_id = _make_version_id()
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        cursor.fetchone.return_value = {
            "id": draft_id,
            "document_id": _make_doc_id(),
            "workflow_status": "proposed",
            "status": "draft",
        }

        with pytest.raises(ApiPermissionDeniedError):
            self.service.approve_draft(
                conn,
                draft_id=draft_id,
                reviewer_id="author-001",
                reviewer_role="AUTHOR",
            )


# ---------------------------------------------------------------------------
# reject_draft 테스트
# ---------------------------------------------------------------------------

class TestRejectDraft:
    def setup_method(self):
        self.service = AgentProposalService()

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_reject_proposed_draft_to_rejected(self, mock_audit):
        """반려 시 Draft의 workflow_status가 rejected로 전환되어야 한다."""
        draft_id = _make_version_id()
        doc_id = _make_doc_id()

        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        cursor.fetchone.return_value = {
            "id": draft_id,
            "document_id": doc_id,
            "workflow_status": "proposed",
            "status": "draft",
        }

        result = self.service.reject_draft(
            conn,
            draft_id=draft_id,
            reviewer_id="reviewer-001",
            reviewer_role="REVIEWER",
            reason="내용 부정확",
        )

        assert result["new_status"] == "rejected"
        assert result["previous_status"] == "proposed"
        assert result["reason"] == "내용 부정확"

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_reject_emits_audit_event(self, mock_audit):
        """반려 시 감사 로그가 actor_type=user로 기록되어야 한다."""
        draft_id = _make_version_id()
        doc_id = _make_doc_id()

        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        cursor.fetchone.return_value = {
            "id": draft_id,
            "document_id": doc_id,
            "workflow_status": "proposed",
            "status": "draft",
        }

        self.service.reject_draft(
            conn,
            draft_id=draft_id,
            reviewer_id="reviewer-001",
            reviewer_role="REVIEWER",
            reason="사유",
        )

        kwargs = mock_audit.emit.call_args[1]
        assert kwargs["event_type"] == "agent.draft.rejected"
        assert kwargs["actor_type"] == "user"
        assert kwargs["new_state"] == "rejected"


# ---------------------------------------------------------------------------
# withdraw_proposal 테스트
# ---------------------------------------------------------------------------

class TestWithdrawProposal:
    def setup_method(self):
        self.service = AgentProposalService()

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_withdraw_sets_withdrawn_status(self, mock_audit):
        """에이전트 회수 시 workflow_status가 withdrawn으로 전환되어야 한다."""
        agent_id = _make_agent_id()
        version_id = _make_version_id()
        doc_id = _make_doc_id()

        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        def fetchone_se(*args, **kwargs):
            sql = cursor.execute.call_args[0][0] if cursor.execute.call_args else ""
            if "agents" in sql and "is_disabled" in sql:
                return {"id": agent_id, "is_disabled": False}
            if "versions" in sql:
                return {
                    "id": version_id,
                    "document_id": doc_id,
                    "workflow_status": "proposed",
                    "created_by": agent_id,
                }
            return None

        cursor.fetchone.side_effect = fetchone_se

        result = self.service.withdraw_proposal(
            conn,
            agent_id=agent_id,
            proposal_id=version_id,
            reason="재검토 필요",
        )

        assert result["new_status"] == "withdrawn"
        assert result["previous_status"] == "proposed"
        assert result["proposal_id"] == version_id

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_withdraw_non_proposed_raises_conflict(self, mock_audit):
        """proposed 상태가 아닌 Draft는 회수할 수 없다."""
        agent_id = _make_agent_id()
        version_id = _make_version_id()

        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        def fetchone_se(*args, **kwargs):
            sql = cursor.execute.call_args[0][0] if cursor.execute.call_args else ""
            if "agents" in sql and "is_disabled" in sql:
                return {"id": agent_id, "is_disabled": False}
            return {
                "id": version_id,
                "document_id": _make_doc_id(),
                "workflow_status": "approved",
                "created_by": agent_id,
            }

        cursor.fetchone.side_effect = fetchone_se

        with pytest.raises(ApiConflictError, match="proposed"):
            self.service.withdraw_proposal(
                conn,
                agent_id=agent_id,
                proposal_id=version_id,
            )

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_withdraw_emits_audit_event(self, mock_audit):
        """회수 시 감사 로그가 actor_type=agent로 기록되어야 한다."""
        agent_id = _make_agent_id()
        version_id = _make_version_id()
        doc_id = _make_doc_id()

        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        def fetchone_se(*args, **kwargs):
            sql = cursor.execute.call_args[0][0] if cursor.execute.call_args else ""
            if "agents" in sql and "is_disabled" in sql:
                return {"id": agent_id, "is_disabled": False}
            return {
                "id": version_id,
                "document_id": doc_id,
                "workflow_status": "proposed",
                "created_by": agent_id,
            }

        cursor.fetchone.side_effect = fetchone_se

        self.service.withdraw_proposal(
            conn,
            agent_id=agent_id,
            proposal_id=version_id,
            reason="회수 사유",
        )

        kwargs = mock_audit.emit.call_args[1]
        assert kwargs["event_type"] == "agent.draft.withdrawn"
        assert kwargs["actor_type"] == "agent"
        assert kwargs["new_state"] == "withdrawn"


# ---------------------------------------------------------------------------
# MCP Task 동기화 테스트
# ---------------------------------------------------------------------------

class TestMcpTaskSync:
    def setup_method(self):
        self.service = AgentProposalService()

    def test_create_mcp_task_returns_id(self):
        """_create_mcp_task가 UUID 형식의 task_id를 반환해야 한다."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        task_id = self.service._create_mcp_task(
            conn,
            title="Test Task",
            description="테스트용",
            reference_type="version",
            reference_id=_make_version_id(),
            agent_id=_make_agent_id(),
        )

        assert task_id is not None
        # UUID 형식 확인
        uuid.UUID(task_id)

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_approve_syncs_mcp_task_to_completed(self, mock_audit):
        """승인 시 MCP Task가 completed로 동기화되어야 한다."""
        draft_id = _make_version_id()
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        cursor.fetchone.return_value = {
            "id": draft_id,
            "document_id": _make_doc_id(),
            "workflow_status": "proposed",
            "status": "draft",
        }

        self.service.approve_draft(
            conn,
            draft_id=draft_id,
            reviewer_id="reviewer-001",
            reviewer_role="APPROVER",
        )

        # mcp_tasks UPDATE가 completed로 호출되었는지 확인
        sync_calls = [
            str(c) for c in cursor.execute.call_args_list
            if "UPDATE mcp_tasks" in str(c) and "completed" in str(c)
        ]
        assert len(sync_calls) >= 1

    @patch("app.services.agent_proposal_service.audit_emitter")
    def test_reject_syncs_mcp_task_to_failed(self, mock_audit):
        """반려 시 MCP Task가 failed로 동기화되어야 한다."""
        draft_id = _make_version_id()
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        cursor.fetchone.return_value = {
            "id": draft_id,
            "document_id": _make_doc_id(),
            "workflow_status": "proposed",
            "status": "draft",
        }

        self.service.reject_draft(
            conn,
            draft_id=draft_id,
            reviewer_id="reviewer-001",
            reviewer_role="REVIEWER",
            reason="사유",
        )

        sync_calls = [
            str(c) for c in cursor.execute.call_args_list
            if "UPDATE mcp_tasks" in str(c) and "failed" in str(c)
        ]
        assert len(sync_calls) >= 1


# ---------------------------------------------------------------------------
# 인간 Draft 경로 불변 검증 (기존 워크플로 테스트)
# ---------------------------------------------------------------------------

class TestHumanWorkflowUnchanged:
    """인간 사용자의 기존 Draft 경로(draft → in_review → approved → published)가
    S2 Phase 5 변경에도 동작하는지 확인한다."""

    def test_human_workflow_statuses_still_exist(self):
        """기존 상태값들이 enum에 여전히 존재해야 한다."""
        assert WorkflowStatus.DRAFT.value == "draft"
        assert WorkflowStatus.IN_REVIEW.value == "in_review"
        assert WorkflowStatus.APPROVED.value == "approved"
        assert WorkflowStatus.PUBLISHED.value == "published"
        assert WorkflowStatus.REJECTED.value == "rejected"
        assert WorkflowStatus.ARCHIVED.value == "archived"

    def test_agent_statuses_are_separate(self):
        """에이전트 상태는 기존 인간 상태와 분리된다."""
        human_statuses = {
            WorkflowStatus.DRAFT,
            WorkflowStatus.IN_REVIEW,
            WorkflowStatus.APPROVED,
            WorkflowStatus.PUBLISHED,
            WorkflowStatus.REJECTED,
            WorkflowStatus.ARCHIVED,
        }
        agent_statuses = {WorkflowStatus.PROPOSED, WorkflowStatus.WITHDRAWN}
        assert human_statuses.isdisjoint(agent_statuses)
