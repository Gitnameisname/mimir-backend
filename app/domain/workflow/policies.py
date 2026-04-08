"""
Workflow 정책 정의.

Phase 5 핵심 정책:
  ALLOWED_TRANSITIONS     : 상태 전이 맵 (State Machine)
  ACTION_TO_TARGET_STATUS : 액션 → 목표 상태 매핑
  WORKFLOW_PERMISSIONS    : 역할 기반 허용 전이 (RBAC)

설계 원칙:
  - 상태 전이 검증과 역할 검증은 별도 단계로 분리한다.
  - 서버에서만 전이를 수행한다 (UI 신뢰 금지).
  - ADMIN은 모든 전이를 허용하되, 감사 로그 필수.
  - 정책 변경은 이 파일에만 집중한다.
"""

from app.domain.workflow.enums import WorkflowAction, WorkflowRole, WorkflowStatus

# ---------------------------------------------------------------------------
# 상태 전이 맵 (Task 5-2)
# ---------------------------------------------------------------------------

ALLOWED_TRANSITIONS: dict[WorkflowStatus, set[WorkflowStatus]] = {
    WorkflowStatus.DRAFT: {
        WorkflowStatus.IN_REVIEW,
    },
    WorkflowStatus.IN_REVIEW: {
        WorkflowStatus.APPROVED,
        WorkflowStatus.REJECTED,
    },
    WorkflowStatus.REJECTED: {
        WorkflowStatus.DRAFT,
    },
    WorkflowStatus.APPROVED: {
        WorkflowStatus.PUBLISHED,
    },
    WorkflowStatus.PUBLISHED: {
        WorkflowStatus.ARCHIVED,
    },
    WorkflowStatus.ARCHIVED: set(),
    # Phase 4 내부 상태 — 워크플로 전이 대상 아님
    WorkflowStatus.SUPERSEDED: set(),
    WorkflowStatus.DISCARDED: set(),
}

# ---------------------------------------------------------------------------
# 액션 → 목표 상태 매핑 (Task 5-4)
# ---------------------------------------------------------------------------

ACTION_TO_TARGET_STATUS: dict[WorkflowAction, WorkflowStatus] = {
    WorkflowAction.SUBMIT_REVIEW: WorkflowStatus.IN_REVIEW,
    WorkflowAction.APPROVE: WorkflowStatus.APPROVED,
    WorkflowAction.REJECT: WorkflowStatus.REJECTED,
    WorkflowAction.PUBLISH: WorkflowStatus.PUBLISHED,
    WorkflowAction.ARCHIVE: WorkflowStatus.ARCHIVED,
    WorkflowAction.RETURN_TO_DRAFT: WorkflowStatus.DRAFT,
}

# ---------------------------------------------------------------------------
# 역할 기반 허용 전이 (Task 5-3)
# ---------------------------------------------------------------------------

# (from_status, to_status) 쌍으로 허용 전이를 정의한다.
WORKFLOW_PERMISSIONS: dict[WorkflowRole, set[tuple[WorkflowStatus, WorkflowStatus]]] = {
    WorkflowRole.AUTHOR: {
        (WorkflowStatus.DRAFT, WorkflowStatus.IN_REVIEW),
        (WorkflowStatus.REJECTED, WorkflowStatus.DRAFT),
    },
    WorkflowRole.REVIEWER: {
        (WorkflowStatus.IN_REVIEW, WorkflowStatus.REJECTED),
    },
    WorkflowRole.APPROVER: {
        (WorkflowStatus.IN_REVIEW, WorkflowStatus.APPROVED),
        (WorkflowStatus.APPROVED, WorkflowStatus.PUBLISHED),
        (WorkflowStatus.PUBLISHED, WorkflowStatus.ARCHIVED),
    },
    # ADMIN은 별도 로직으로 모든 전이 허용 (아래 헬퍼 참고)
    WorkflowRole.ADMIN: set(),
}

# ---------------------------------------------------------------------------
# 헬퍼 함수
# ---------------------------------------------------------------------------


def is_transition_allowed(
    current: WorkflowStatus,
    target: WorkflowStatus,
) -> bool:
    """상태 전이 허용 여부를 반환한다 (역할 무관)."""
    return target in ALLOWED_TRANSITIONS.get(current, set())


def is_role_allowed(
    role: WorkflowRole,
    current: WorkflowStatus,
    target: WorkflowStatus,
) -> bool:
    """역할 기반 상태 전이 허용 여부를 반환한다.

    ADMIN은 모든 전이를 허용한다.
    다른 역할은 WORKFLOW_PERMISSIONS 맵을 기준으로 판단한다.
    """
    if role == WorkflowRole.ADMIN:
        return True
    return (current, target) in WORKFLOW_PERMISSIONS.get(role, set())


def get_target_status(action: WorkflowAction) -> WorkflowStatus:
    """액션에서 목표 상태를 반환한다."""
    return ACTION_TO_TARGET_STATUS[action]


# ---------------------------------------------------------------------------
# 상태별 정책 플래그
# ---------------------------------------------------------------------------

# 직접 수정(content 편집)이 허용되는 상태
EDITABLE_STATUSES: frozenset[WorkflowStatus] = frozenset({
    WorkflowStatus.DRAFT,
    WorkflowStatus.REJECTED,
})

# 외부 열람이 허용되는 상태
PUBLIC_STATUSES: frozenset[WorkflowStatus] = frozenset({
    WorkflowStatus.PUBLISHED,
})

# 워크플로 전이가 불가한 최종 상태
TERMINAL_STATUSES: frozenset[WorkflowStatus] = frozenset({
    WorkflowStatus.ARCHIVED,
    WorkflowStatus.SUPERSEDED,
    WorkflowStatus.DISCARDED,
})
