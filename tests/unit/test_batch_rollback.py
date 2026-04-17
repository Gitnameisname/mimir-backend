"""
Batch 롤백 서비스 단위 테스트 — PH6-CARRY-001 (task7-11)
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch
from uuid import uuid4

import pytest

from app.api.errors.exceptions import ApiPermissionDeniedError
from app.services.agent_proposal_service import AgentProposalService


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_proposal_row(
    pid: str,
    status: str = "approved",
    proposal_type: str = "draft",
    workflow_status: str = "approved",
) -> dict:
    return {
        "id": pid,
        "status": status,
        "reference_id": str(uuid4()),
        "proposal_type": proposal_type,
        "workflow_status": workflow_status,
    }


def _mock_conn(rows: list[dict | None]):
    """rows: fetchone 순서대로 반환할 값 목록."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: cur
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur
    cur.fetchone.side_effect = rows
    return conn, cur


# ---------------------------------------------------------------------------
# TestBatchRollback
# ---------------------------------------------------------------------------

class TestBatchRollback:
    """AgentProposalService.batch_rollback 단위 테스트."""

    @pytest.fixture
    def svc(self):
        return AgentProposalService()

    def _run(
        self,
        svc,
        conn,
        ids,
        original_action="approve",
        role="ORG_ADMIN",
    ):
        with patch("app.services.agent_proposal_service.audit_emitter"):
            return svc.batch_rollback(
                conn,
                proposal_ids=ids,
                original_action=original_action,
                reviewer_id="reviewer-1",
                reviewer_role=role,
            )

    # --- permission ---

    def test_raises_for_non_reviewer_role(self, svc):
        conn, _ = _mock_conn([])
        with pytest.raises(ApiPermissionDeniedError):
            self._run(svc, conn, ["id1"], role="VIEWER")

    def test_allows_org_admin(self, svc):
        pid = str(uuid4())
        row = _make_proposal_row(pid, status="approved", workflow_status="approved")
        conn, _ = _mock_conn([row])
        result = self._run(svc, conn, [pid], original_action="approve", role="ORG_ADMIN")
        assert result["rolled_back"] == 1

    # --- normal rollback ---

    def test_rolls_back_approved_proposal(self, svc):
        pid = str(uuid4())
        row = _make_proposal_row(pid, status="approved", workflow_status="approved")
        conn, _ = _mock_conn([row])
        result = self._run(svc, conn, [pid], original_action="approve")
        assert result["rolled_back"] == 1
        assert result["skipped"] == 0

    def test_rolls_back_rejected_proposal(self, svc):
        pid = str(uuid4())
        row = _make_proposal_row(pid, status="rejected", workflow_status="rejected")
        conn, _ = _mock_conn([row])
        result = self._run(svc, conn, [pid], original_action="reject")
        assert result["rolled_back"] == 1
        assert result["skipped"] == 0

    # --- skip conditions ---

    def test_skips_committed_version(self, svc):
        pid = str(uuid4())
        row = _make_proposal_row(pid, status="approved", workflow_status="committed")
        conn, _ = _mock_conn([row])
        result = self._run(svc, conn, [pid], original_action="approve")
        assert result["rolled_back"] == 0
        assert result["skipped"] == 1
        assert pid in result["skipped_ids"]

    def test_skips_status_mismatch(self, svc):
        """original_action=approve 지만 status=rejected 인 경우."""
        pid = str(uuid4())
        row = _make_proposal_row(pid, status="rejected", workflow_status="rejected")
        conn, _ = _mock_conn([row])
        result = self._run(svc, conn, [pid], original_action="approve")
        assert result["skipped"] == 1
        assert result["rolled_back"] == 0

    def test_skips_not_found_proposal(self, svc):
        pid = str(uuid4())
        conn, _ = _mock_conn([None])
        result = self._run(svc, conn, [pid], original_action="approve")
        assert result["skipped"] == 1

    # --- mixed batch ---

    def test_mixed_batch(self, svc):
        pid_ok = str(uuid4())
        pid_skip = str(uuid4())
        rows = [
            _make_proposal_row(pid_ok, status="approved", workflow_status="approved"),
            _make_proposal_row(pid_skip, status="approved", workflow_status="committed"),
        ]
        conn, _ = _mock_conn(rows)
        result = self._run(svc, conn, [pid_ok, pid_skip], original_action="approve")
        assert result["rolled_back"] == 1
        assert result["skipped"] == 1
        assert pid_skip in result["skipped_ids"]

    # --- empty input ---

    def test_empty_ids(self, svc):
        conn, _ = _mock_conn([])
        result = self._run(svc, conn, [], original_action="approve")
        assert result["rolled_back"] == 0
        assert result["skipped"] == 0

    # --- audit ---

    def test_audit_emitted_per_rollback(self, svc):
        pid = str(uuid4())
        row = _make_proposal_row(pid, status="approved", workflow_status="approved")
        conn, _ = _mock_conn([row])
        with patch("app.services.agent_proposal_service.audit_emitter") as mock_audit:
            svc.batch_rollback(
                conn,
                proposal_ids=[pid],
                original_action="approve",
                reviewer_id="r1",
                reviewer_role="ORG_ADMIN",
            )
        mock_audit.emit.assert_called_once()
        kwargs = mock_audit.emit.call_args.kwargs
        assert kwargs["event_type"] == "agent.proposal.rolled_back"
        assert kwargs["previous_state"] == "approved"
        assert kwargs["new_state"] == "pending"
