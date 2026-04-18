"""
WorkflowService — Phase 5 워크플로 비즈니스 로직.

책임:
  - 워크플로 액션 수행 (submit_review / approve / reject / publish / archive / return_to_draft)
  - 상태 전이 유효성 검증 (ALLOWED_TRANSITIONS 기반)
  - RBAC 기반 권한 검증 (WORKFLOW_PERMISSIONS 기반)
  - ReviewAction 기록
  - WorkflowHistory 기록
  - ChangeLog 기록
  - audit_emitter 연동

설계 원칙 (Task 5-4):
  - 라우터는 얇게 유지. 비즈니스 로직은 이 서비스에만 집중.
  - 상태 전이, 권한 검증, 이력 기록을 하나의 트랜잭션으로 묶는다.
  - ADMIN 역할은 모든 전이를 허용하되 감사 로그 필수.
  - expected_current_status로 낙관적 동시성 충돌을 감지한다.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg2.extensions

from app.api.errors.exceptions import (
    ApiConflictError,
    ApiNotFoundError,
    ApiPermissionDeniedError,
)
from app.audit.emitter import audit_emitter
from app.domain.workflow.enums import WorkflowAction, WorkflowRole, WorkflowStatus
from app.domain.workflow.policies import (
    get_target_status,
    is_role_allowed,
    is_transition_allowed,
)
from app.repositories.documents_repository import documents_repository
from app.repositories.versions_repository import versions_repository
from app.repositories.workflow_repository import workflow_repository

logger = logging.getLogger(__name__)

# 문자열 역할 → WorkflowRole Enum 변환 테이블.
# WorkflowRole 자체 값 외에 Phase 2 RBAC 역할명(대/소문자 무관)도 포함한다.
_ROLE_MAP: dict[str, WorkflowRole] = {
    # WorkflowRole 직접 값 (소문자)
    **{r.value: r for r in WorkflowRole},
    # Phase 2 RBAC 역할 → WorkflowRole 매핑
    "viewer":      WorkflowRole.AUTHOR,   # 뷰어는 최소 권한(AUTHOR)으로 처리; RBAC에서 차단됨
    "org_admin":   WorkflowRole.ADMIN,
    "super_admin": WorkflowRole.ADMIN,
}


def _resolve_role(role_str: Optional[str]) -> WorkflowRole:
    """문자열 역할을 WorkflowRole Enum으로 변환한다.

    WorkflowRole 값(author/reviewer/approver/admin)과
    Phase 2 RBAC 역할명(VIEWER/AUTHOR/REVIEWER/APPROVER/ORG_ADMIN/SUPER_ADMIN)
    을 모두 처리한다. 알 수 없는 역할은 AUTHOR로 폴백.
    """
    if role_str is None:
        return WorkflowRole.AUTHOR
    return _ROLE_MAP.get(role_str.lower(), WorkflowRole.AUTHOR)


def _resolve_current_workflow_status(version_status: str, workflow_status: Optional[str]) -> WorkflowStatus:
    """버전의 현재 워크플로 상태를 결정한다.

    workflow_status 컬럼이 있으면 우선 사용.
    없으면 기존 status 값으로 폴백.
    """
    raw = workflow_status if workflow_status else version_status
    try:
        return WorkflowStatus(raw)
    except ValueError:
        # 알 수 없는 상태 — DRAFT로 안전 폴백
        logger.warning("Unknown workflow status '%s', falling back to DRAFT", raw)
        return WorkflowStatus.DRAFT


class WorkflowService:
    """워크플로 액션 처리 서비스."""

    # -----------------------------------------------------------------------
    # 핵심 액션 수행
    # -----------------------------------------------------------------------

    def perform_action(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        version_id: str,
        action: WorkflowAction,
        actor_id: Optional[str] = None,
        actor_role: Optional[str] = None,
        comment: Optional[str] = None,
        reason: Optional[str] = None,
        expected_current_status: Optional[str] = None,
        request_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """워크플로 액션을 수행하고 결과를 반환한다.

        처리 순서:
          1. 문서/버전 존재 여부 확인
          2. 현재 workflow_status 조회
          3. expected_current_status 일치 확인 (낙관적 락)
          4. 상태 전이 허용 여부 검증 (Task 5-2)
          5. 역할 기반 권한 검증 (Task 5-3)
          6. 상태 전이 수행 (versions.workflow_status 갱신)
          7. ReviewAction 기록
          8. WorkflowHistory 기록
          9. ChangeLog 기록
          10. Audit Log emit

        Returns:
            {document_id, version_id, previous_status, current_status, action, acted_by, acted_at}
        """
        # 0. 운영 정책 검증 (Task 5-9)
        # reject 시 reason 필수 (Task 5-5 §12.1)
        if action == WorkflowAction.REJECT and not reason:
            from app.api.errors.exceptions import ApiValidationError
            raise ApiValidationError(
                "A rejection reason is required.",
                details=[{"field": "reason", "error_code": "REASON_REQUIRED"}],
            )

        # 1. 문서/버전 존재 확인
        doc = documents_repository.get_by_id(conn, document_id)
        if doc is None:
            raise ApiNotFoundError(f"Document '{document_id}' not found")

        version = versions_repository.get_by_document_and_version_id(
            conn, document_id, version_id
        )
        if version is None:
            raise ApiNotFoundError(
                f"Version '{version_id}' not found in document '{document_id}'"
            )

        # 2. 현재 workflow_status 조회
        raw_wf_status = workflow_repository.get_workflow_status(conn, version_id)
        current_status = _resolve_current_workflow_status(version.status, raw_wf_status)

        # 3. 낙관적 락: expected_current_status 불일치 검사
        if expected_current_status is not None:
            try:
                expected = WorkflowStatus(expected_current_status)
            except ValueError:
                raise ApiConflictError(
                    f"Invalid expected_current_status value: '{expected_current_status}'"
                )
            if current_status != expected:
                raise ApiConflictError(
                    f"Workflow state conflict: expected '{expected.value}' "
                    f"but current status is '{current_status.value}'",
                    details={"error_code": "WORKFLOW_STATE_CONFLICT"},
                )

        # 4. 상태 전이 허용 여부 검증
        target_status = get_target_status(action)
        if not is_transition_allowed(current_status, target_status):
            raise ApiConflictError(
                f"Cannot transition from '{current_status.value}' to '{target_status.value}'",
                details={"error_code": "INVALID_WORKFLOW_TRANSITION"},
            )

        # 5. RBAC 권한 검증
        role = _resolve_role(actor_role)
        if not is_role_allowed(role, current_status, target_status):
            raise ApiPermissionDeniedError(
                f"Role '{role.value}' is not permitted to perform '{action.value}'",
            )

        # 6. 상태 전이 수행
        workflow_repository.update_workflow_status(conn, version_id, target_status.value)

        # PUBLISH 전이 시 versions.status / published_by / published_at 동기화,
        # documents.current_published_version_id 및 documents.status 업데이트
        if target_status == WorkflowStatus.PUBLISHED:
            now_ts = datetime.now(timezone.utc)
            versions_repository.update_status(
                conn,
                version_id,
                status="published",
                published_by=actor_id,
                published_at=now_ts,
            )
            documents_repository.update_version_pointers(
                conn,
                document_id,
                current_published_version_id=version_id,
            )
            # 문서 자체 status도 published로 갱신
            with conn.cursor() as _cur:
                _cur.execute(
                    "UPDATE documents SET status = 'published', updated_at = %s WHERE id = %s",
                    (now_ts, document_id),
                )

        # admin override 여부 플래그
        is_admin_override = (role == WorkflowRole.ADMIN)

        # 7. ReviewAction 기록 (행위 시점 역할 스냅샷 포함)
        workflow_repository.create_review_action(
            conn,
            document_id=document_id,
            version_id=version_id,
            action_type=action.value,
            from_status=current_status.value,
            to_status=target_status.value,
            actor_id=actor_id,
            actor_role=actor_role,
            comment=comment,
            reason=reason,
            metadata={"is_admin_override": is_admin_override} if is_admin_override else None,
        )

        # 8. WorkflowHistory 기록 (행위 시점 역할 스냅샷 포함)
        workflow_repository.create_workflow_history(
            conn,
            document_id=document_id,
            version_id=version_id,
            from_status=current_status.value,
            to_status=target_status.value,
            action=action.value,
            actor_id=actor_id,
            actor_role=actor_role,
            comment=comment,
            reason=reason,
        )

        # 9. ChangeLog 기록 (이유가 있을 때만, Task 5-6)
        if reason or comment:
            workflow_repository.create_change_log(
                conn,
                document_id=document_id,
                version_id=version_id,
                change_type=f"workflow_transition.{action.value}",
                actor_id=actor_id,
                actor_role=actor_role,
                reason=reason or comment,
                metadata={
                    "from_status": current_status.value,
                    "to_status": target_status.value,
                    "action": action.value,
                    "is_admin_override": is_admin_override,
                },
            )

        acted_at = datetime.now(timezone.utc)

        # 10. Audit Log emit (Admin override 시 명시 기록, Task 5-9 §6)
        audit_emitter.emit(
            event_type=f"document.workflow.{action.value}",
            action=f"workflow.{action.value}",
            actor_id=actor_id,
            actor_role=actor_role,
            resource_type="version",
            resource_id=version_id,
            result="success",
            request_id=request_id,
            trace_id=trace_id,
            metadata={
                "document_id": document_id,
                "version_number": version.version_number,
                "is_admin_override": is_admin_override,
            },
            previous_state=current_status.value,
            new_state=target_status.value,
        )

        logger.info(
            "Workflow action performed: doc=%s ver=%s action=%s %s→%s actor=%s",
            document_id, version_id, action.value,
            current_status.value, target_status.value, actor_id,
        )

        return {
            "document_id": document_id,
            "version_id": version_id,
            "version_number": version.version_number,
            "previous_status": current_status.value,
            "current_status": target_status.value,
            "action": action.value,
            "acted_by": actor_id,
            "acted_at": acted_at.isoformat(),
            "comment": comment,
            "reason": reason,
        }

    # -----------------------------------------------------------------------
    # 개별 액션 편의 메서드
    # -----------------------------------------------------------------------

    def submit_review(self, conn, document_id, version_id, *, actor_id=None, actor_role=None,
                      comment=None, reason=None, expected_current_status=None,
                      request_id=None, trace_id=None):
        return self.perform_action(
            conn, document_id, version_id,
            action=WorkflowAction.SUBMIT_REVIEW,
            actor_id=actor_id, actor_role=actor_role,
            comment=comment, reason=reason,
            expected_current_status=expected_current_status,
            request_id=request_id, trace_id=trace_id,
        )

    def approve(self, conn, document_id, version_id, *, actor_id=None, actor_role=None,
                comment=None, reason=None, expected_current_status=None,
                request_id=None, trace_id=None):
        return self.perform_action(
            conn, document_id, version_id,
            action=WorkflowAction.APPROVE,
            actor_id=actor_id, actor_role=actor_role,
            comment=comment, reason=reason,
            expected_current_status=expected_current_status,
            request_id=request_id, trace_id=trace_id,
        )

    def reject(self, conn, document_id, version_id, *, actor_id=None, actor_role=None,
               comment=None, reason=None, expected_current_status=None,
               request_id=None, trace_id=None):
        return self.perform_action(
            conn, document_id, version_id,
            action=WorkflowAction.REJECT,
            actor_id=actor_id, actor_role=actor_role,
            comment=comment, reason=reason,
            expected_current_status=expected_current_status,
            request_id=request_id, trace_id=trace_id,
        )

    def publish(self, conn, document_id, version_id, *, actor_id=None, actor_role=None,
                comment=None, reason=None, expected_current_status=None,
                request_id=None, trace_id=None):
        return self.perform_action(
            conn, document_id, version_id,
            action=WorkflowAction.PUBLISH,
            actor_id=actor_id, actor_role=actor_role,
            comment=comment, reason=reason,
            expected_current_status=expected_current_status,
            request_id=request_id, trace_id=trace_id,
        )

    def archive(self, conn, document_id, version_id, *, actor_id=None, actor_role=None,
                comment=None, reason=None, expected_current_status=None,
                request_id=None, trace_id=None):
        return self.perform_action(
            conn, document_id, version_id,
            action=WorkflowAction.ARCHIVE,
            actor_id=actor_id, actor_role=actor_role,
            comment=comment, reason=reason,
            expected_current_status=expected_current_status,
            request_id=request_id, trace_id=trace_id,
        )

    def return_to_draft(self, conn, document_id, version_id, *, actor_id=None, actor_role=None,
                        comment=None, reason=None, expected_current_status=None,
                        request_id=None, trace_id=None):
        return self.perform_action(
            conn, document_id, version_id,
            action=WorkflowAction.RETURN_TO_DRAFT,
            actor_id=actor_id, actor_role=actor_role,
            comment=comment, reason=reason,
            expected_current_status=expected_current_status,
            request_id=request_id, trace_id=trace_id,
        )

    # -----------------------------------------------------------------------
    # 이력 조회
    # -----------------------------------------------------------------------

    def get_history(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        version_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list, int]:
        """워크플로 이력을 반환한다."""
        doc = documents_repository.get_by_id(conn, document_id)
        if doc is None:
            raise ApiNotFoundError(f"Document '{document_id}' not found")

        return workflow_repository.list_workflow_history(
            conn, document_id, version_id=version_id, limit=limit, offset=offset
        )

    def get_review_actions(
        self,
        conn: psycopg2.extensions.connection,
        version_id: str,
    ) -> list:
        """특정 버전의 review_actions를 반환한다."""
        return workflow_repository.list_review_actions(conn, version_id)


workflow_service = WorkflowService()
