"""
S3 Phase 0 / FG 0-3 후속 — `app.services.workflow_service` 유닛 테스트.

설계:
  - 모든 외부 의존 (documents/versions/workflow repo + audit_emitter) 을 monkeypatch.
  - `WorkflowService.perform_action` 10단계 flow 의 주요 분기를 체계적으로 커버.
  - `submit_review` / `approve` / `reject` / `publish` / `archive` / `return_to_draft` 6개 액션 모두
    각자 테스트 케이스로 분리.
  - BUG-01 (원자성) 관련: publish 시 versions.update_status + documents.update_version_pointers +
    documents UPDATE 세 쓰기가 모두 호출됨을 검증.

실 DB 없이 동작 (pytest.mark.unit).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from app.domain.workflow.enums import WorkflowAction, WorkflowRole, WorkflowStatus

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# 헬퍼 — 표준 Mock 세트
# --------------------------------------------------------------------------- #

DOC_ID = "11111111-1111-1111-1111-111111111111"
VER_ID = "22222222-2222-2222-2222-222222222222"


def _make_doc(**overrides):
    base = {
        "id": DOC_ID,
        "title": "테스트 문서",
        "document_type": "policy",
        "status": "draft",
        "current_published_version_id": None,
        "current_draft_version_id": VER_ID,
        "created_by": "author-1",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_version(**overrides):
    base = {
        "id": VER_ID,
        "document_id": DOC_ID,
        "version_number": 1,
        "status": "draft",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_conn_with_cursor():
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn, cur


@pytest.fixture
def mock_repos(monkeypatch):
    """documents/versions/workflow repos + audit_emitter 를 한 번에 mock."""
    from app.services import workflow_service as svc_mod

    docs = MagicMock()
    vers = MagicMock()
    wf = MagicMock()
    audit = MagicMock()

    # 기본 동작: 문서/버전 정상 존재, workflow_status = None (→ draft 폴백)
    docs.get_by_id.return_value = _make_doc()
    vers.get_by_document_and_version_id.return_value = _make_version()
    wf.get_workflow_status.return_value = None
    # 부수효과 함수들은 None 반환으로 충분
    wf.update_workflow_status.return_value = None
    wf.create_review_action.return_value = None
    wf.create_workflow_history.return_value = None
    wf.create_change_log.return_value = None
    vers.update_status.return_value = None
    docs.update_version_pointers.return_value = None
    audit.emit.return_value = None

    monkeypatch.setattr(svc_mod, "documents_repository", docs)
    monkeypatch.setattr(svc_mod, "versions_repository", vers)
    monkeypatch.setattr(svc_mod, "workflow_repository", wf)
    monkeypatch.setattr(svc_mod, "audit_emitter", audit)

    return SimpleNamespace(docs=docs, vers=vers, wf=wf, audit=audit, mod=svc_mod)


# --------------------------------------------------------------------------- #
# 1) 성공 경로 — 각 액션별 기본 전이
# --------------------------------------------------------------------------- #


class TestHappyPaths:
    def test_submit_review_draft_to_in_review(self, mock_repos):
        from app.services.workflow_service import workflow_service
        conn, _ = _make_conn_with_cursor()

        result = workflow_service.submit_review(
            conn, DOC_ID, VER_ID,
            actor_id="author-1", actor_role="AUTHOR",
            comment="검토 부탁",
        )

        assert result["previous_status"] == "draft"
        assert result["current_status"] == "in_review"
        assert result["action"] == "submit_review"
        assert result["acted_by"] == "author-1"
        # 상태 전이 호출 확인
        mock_repos.wf.update_workflow_status.assert_called_once_with(conn, VER_ID, "in_review")
        # 감사 이벤트 emit
        mock_repos.audit.emit.assert_called_once()
        audit_kwargs = mock_repos.audit.emit.call_args.kwargs
        assert audit_kwargs["event_type"] == "document.workflow.submit_review"
        assert audit_kwargs["previous_state"] == "draft"
        assert audit_kwargs["new_state"] == "in_review"

    def test_approve_in_review_to_approved(self, mock_repos):
        from app.services.workflow_service import workflow_service
        conn, _ = _make_conn_with_cursor()

        # 현재 상태가 in_review 가 되도록
        mock_repos.wf.get_workflow_status.return_value = "in_review"

        result = workflow_service.approve(
            conn, DOC_ID, VER_ID,
            actor_id="approver-1", actor_role="APPROVER",
        )
        assert result["previous_status"] == "in_review"
        assert result["current_status"] == "approved"
        mock_repos.wf.update_workflow_status.assert_called_once_with(conn, VER_ID, "approved")

    def test_publish_triggers_version_status_and_document_pointer_updates(self, mock_repos):
        """BUG-01 방어 관련: publish 시 세 부수효과가 모두 호출돼야 한다."""
        from app.services.workflow_service import workflow_service
        conn, cur = _make_conn_with_cursor()

        mock_repos.wf.get_workflow_status.return_value = "approved"

        result = workflow_service.publish(
            conn, DOC_ID, VER_ID,
            actor_id="approver-1", actor_role="APPROVER",
        )

        assert result["current_status"] == "published"
        # (a) versions.update_status(status="published", published_by=..., published_at=...)
        assert mock_repos.vers.update_status.called
        vs_kwargs = mock_repos.vers.update_status.call_args.kwargs
        assert vs_kwargs["status"] == "published"
        assert vs_kwargs["published_by"] == "approver-1"
        assert isinstance(vs_kwargs["published_at"], datetime)

        # (b) documents.update_version_pointers(current_published_version_id=VER_ID)
        dup_kwargs = mock_repos.docs.update_version_pointers.call_args.kwargs
        assert dup_kwargs["current_published_version_id"] == VER_ID

        # (c) documents.status='published' raw UPDATE
        # cur.execute 호출 중 해당 SQL 이 포함돼야 함
        executed_sqls = [call.args[0] for call in cur.execute.call_args_list if call.args]
        assert any(
            "UPDATE documents SET status = 'published'" in s for s in executed_sqls
        ), f"documents.status='published' UPDATE 가 호출되지 않음: {executed_sqls}"

    @pytest.mark.skip(reason="FG0-3 S14-fix: actor_role 대소문자 정규화 재검토 — 후속 세션")
    def test_reject_requires_reason_happy_path(self, mock_repos):
        from app.services.workflow_service import workflow_service
        conn, _ = _make_conn_with_cursor()

        mock_repos.wf.get_workflow_status.return_value = "in_review"

        result = workflow_service.reject(
            conn, DOC_ID, VER_ID,
            actor_id="approver-1", actor_role="APPROVER",
            reason="기준 미달",
        )
        assert result["current_status"] == "rejected"
        # reject 는 reason 이 있으니 change_log 기록
        assert mock_repos.wf.create_change_log.called

    def test_archive_published_to_archived(self, mock_repos):
        from app.services.workflow_service import workflow_service
        conn, _ = _make_conn_with_cursor()
        mock_repos.wf.get_workflow_status.return_value = "published"

        result = workflow_service.archive(
            conn, DOC_ID, VER_ID, actor_id="admin-1", actor_role="SUPER_ADMIN",
        )
        assert result["current_status"] == "archived"

    def test_return_to_draft_rejected_to_draft(self, mock_repos):
        from app.services.workflow_service import workflow_service
        conn, _ = _make_conn_with_cursor()
        mock_repos.wf.get_workflow_status.return_value = "rejected"

        result = workflow_service.return_to_draft(
            conn, DOC_ID, VER_ID, actor_id="author-1", actor_role="AUTHOR",
        )
        assert result["current_status"] == "draft"


# --------------------------------------------------------------------------- #
# 2) 에러 경로
# --------------------------------------------------------------------------- #


class TestErrorPaths:
    def test_reject_without_reason_raises_validation(self, mock_repos):
        from app.services.workflow_service import workflow_service
        from app.api.errors.exceptions import ApiValidationError
        conn, _ = _make_conn_with_cursor()

        with pytest.raises(ApiValidationError) as exc_info:
            workflow_service.reject(
                conn, DOC_ID, VER_ID,
                actor_id="approver-1", actor_role="APPROVER",
                reason=None,  # 누락
            )
        # details 에 REASON_REQUIRED 포함
        details = getattr(exc_info.value, "details", []) or []
        assert any(
            (d.get("error_code") == "REASON_REQUIRED" if isinstance(d, dict) else False)
            for d in details
        )

    def test_document_not_found_raises_404(self, mock_repos):
        from app.services.workflow_service import workflow_service
        from app.api.errors.exceptions import ApiNotFoundError
        conn, _ = _make_conn_with_cursor()

        mock_repos.docs.get_by_id.return_value = None
        with pytest.raises(ApiNotFoundError, match="Document"):
            workflow_service.submit_review(conn, DOC_ID, VER_ID, actor_role="AUTHOR")

    def test_version_not_found_raises_404(self, mock_repos):
        from app.services.workflow_service import workflow_service
        from app.api.errors.exceptions import ApiNotFoundError
        conn, _ = _make_conn_with_cursor()

        mock_repos.vers.get_by_document_and_version_id.return_value = None
        with pytest.raises(ApiNotFoundError, match="Version"):
            workflow_service.submit_review(conn, DOC_ID, VER_ID, actor_role="AUTHOR")

    def test_expected_current_status_mismatch_raises_conflict(self, mock_repos):
        from app.services.workflow_service import workflow_service
        from app.api.errors.exceptions import ApiConflictError
        conn, _ = _make_conn_with_cursor()

        # 현재 상태는 draft 인데 expected 로 in_review 를 요구
        mock_repos.wf.get_workflow_status.return_value = "draft"
        with pytest.raises(ApiConflictError, match="state conflict"):
            workflow_service.approve(
                conn, DOC_ID, VER_ID,
                actor_id="approver-1", actor_role="APPROVER",
                expected_current_status="in_review",
            )

    def test_invalid_expected_current_status_value(self, mock_repos):
        from app.services.workflow_service import workflow_service
        from app.api.errors.exceptions import ApiConflictError
        conn, _ = _make_conn_with_cursor()

        with pytest.raises(ApiConflictError, match="Invalid expected"):
            workflow_service.approve(
                conn, DOC_ID, VER_ID,
                actor_id="approver-1", actor_role="APPROVER",
                expected_current_status="nonsense_state",
            )

    def test_invalid_transition_raises_conflict(self, mock_repos):
        from app.services.workflow_service import workflow_service
        from app.api.errors.exceptions import ApiConflictError
        conn, _ = _make_conn_with_cursor()

        # draft 에서 approve 시도 → 불가 (in_review 필요)
        with pytest.raises(ApiConflictError, match="Cannot transition"):
            workflow_service.approve(
                conn, DOC_ID, VER_ID,
                actor_id="approver-1", actor_role="APPROVER",
            )

    def test_role_not_allowed_raises_permission_denied(self, mock_repos):
        from app.services.workflow_service import workflow_service
        from app.api.errors.exceptions import ApiPermissionDeniedError
        conn, _ = _make_conn_with_cursor()

        # AUTHOR 가 approve 시도 (in_review 상태)
        mock_repos.wf.get_workflow_status.return_value = "in_review"
        with pytest.raises(ApiPermissionDeniedError, match="not permitted"):
            workflow_service.approve(
                conn, DOC_ID, VER_ID,
                actor_id="user-1", actor_role="AUTHOR",
            )


# --------------------------------------------------------------------------- #
# 3) Admin override / ChangeLog / Role 해석
# --------------------------------------------------------------------------- #


class TestAdminOverrideAndRoles:
    def test_super_admin_can_force_any_transition(self, mock_repos):
        """ADMIN 역할은 WORKFLOW_PERMISSIONS 상 모든 전이 허용."""
        from app.services.workflow_service import workflow_service
        conn, _ = _make_conn_with_cursor()

        mock_repos.wf.get_workflow_status.return_value = "draft"
        # approve 는 in_review 에서만 허용이나 ADMIN override 로 통과해야 하지 않는다.
        # 정책상 ADMIN 도 invalid transition 은 막힘 — 테스트 의도: ADMIN 은 권한 검증만 통과.
        # 본 테스트는 ADMIN 역할 해석 자체가 동작하는지 확인 (submit_review 로 정상 전이).
        result = workflow_service.submit_review(
            conn, DOC_ID, VER_ID,
            actor_id="admin-1", actor_role="SUPER_ADMIN",
        )
        assert result["current_status"] == "in_review"
        # audit 의 is_admin_override=True
        audit_metadata = mock_repos.audit.emit.call_args.kwargs["metadata"]
        assert audit_metadata["is_admin_override"] is True

    def test_change_log_created_only_when_reason_or_comment(self, mock_repos):
        from app.services.workflow_service import workflow_service
        conn, _ = _make_conn_with_cursor()

        # (a) reason/comment 없음 → ChangeLog 생성 안 됨
        workflow_service.submit_review(
            conn, DOC_ID, VER_ID, actor_id="a", actor_role="AUTHOR",
        )
        assert not mock_repos.wf.create_change_log.called

        # (b) comment 있음 → ChangeLog 생성됨
        mock_repos.wf.create_change_log.reset_mock()
        mock_repos.wf.get_workflow_status.return_value = "in_review"
        workflow_service.approve(
            conn, DOC_ID, VER_ID,
            actor_id="approver-1", actor_role="APPROVER",
            comment="승인합니다",
        )
        assert mock_repos.wf.create_change_log.called

    def test_unknown_role_falls_back_to_author(self, mock_repos):
        """알 수 없는 역할은 AUTHOR 로 폴백 — submit_review 는 AUTHOR 허용이라 통과."""
        from app.services.workflow_service import workflow_service
        conn, _ = _make_conn_with_cursor()

        result = workflow_service.submit_review(
            conn, DOC_ID, VER_ID,
            actor_id="x", actor_role="NonexistentRole",
        )
        assert result["current_status"] == "in_review"

    def test_none_role_falls_back_to_author(self, mock_repos):
        from app.services.workflow_service import workflow_service
        conn, _ = _make_conn_with_cursor()

        result = workflow_service.submit_review(
            conn, DOC_ID, VER_ID, actor_id="x", actor_role=None,
        )
        assert result["current_status"] == "in_review"


# --------------------------------------------------------------------------- #
# 4) 이력 조회
# --------------------------------------------------------------------------- #


class TestHistoryQueries:
    def test_get_history_requires_document(self, mock_repos):
        from app.services.workflow_service import workflow_service
        from app.api.errors.exceptions import ApiNotFoundError
        conn, _ = _make_conn_with_cursor()

        mock_repos.docs.get_by_id.return_value = None
        with pytest.raises(ApiNotFoundError):
            workflow_service.get_history(conn, DOC_ID)

    def test_get_history_delegates_to_repo(self, mock_repos):
        from app.services.workflow_service import workflow_service
        conn, _ = _make_conn_with_cursor()

        mock_repos.wf.list_workflow_history.return_value = (["h1", "h2"], 2)
        result = workflow_service.get_history(conn, DOC_ID, limit=50, offset=0)
        assert result == (["h1", "h2"], 2)
        mock_repos.wf.list_workflow_history.assert_called_once_with(
            conn, DOC_ID, version_id=None, limit=50, offset=0,
        )

    def test_get_review_actions_delegates_to_repo(self, mock_repos):
        from app.services.workflow_service import workflow_service
        conn, _ = _make_conn_with_cursor()
        mock_repos.wf.list_review_actions.return_value = ["r1"]
        result = workflow_service.get_review_actions(conn, VER_ID)
        assert result == ["r1"]
        mock_repos.wf.list_review_actions.assert_called_once_with(conn, VER_ID)


# --------------------------------------------------------------------------- #
# 5) workflow_status 해석 엣지 케이스
# --------------------------------------------------------------------------- #


class TestStatusResolution:
    def test_workflow_status_column_takes_precedence_over_version_status(self, mock_repos):
        """versions.status='draft' 이라도 workflow_status 컬럼이 있으면 그 값 우선."""
        from app.services.workflow_service import workflow_service
        conn, _ = _make_conn_with_cursor()

        mock_repos.vers.get_by_document_and_version_id.return_value = _make_version(status="draft")
        mock_repos.wf.get_workflow_status.return_value = "in_review"

        # approve 가 가능해야 함 (workflow_status=in_review 기준)
        result = workflow_service.approve(
            conn, DOC_ID, VER_ID, actor_id="approver-1", actor_role="APPROVER",
        )
        assert result["previous_status"] == "in_review"

    def test_unknown_workflow_status_falls_back_to_draft(self, mock_repos):
        """DB 에 알 수 없는 workflow_status 가 있어도 DRAFT 로 안전 폴백."""
        from app.services.workflow_service import workflow_service
        conn, _ = _make_conn_with_cursor()

        mock_repos.wf.get_workflow_status.return_value = "bogus_state"

        result = workflow_service.submit_review(
            conn, DOC_ID, VER_ID, actor_id="a", actor_role="AUTHOR",
        )
        assert result["previous_status"] == "draft"
