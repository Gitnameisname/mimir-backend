"""
Workflow 도메인 Enum 정의.

Phase 5 핵심 열거형:
  - WorkflowStatus : 버전 단위 워크플로 상태 (DRAFT → IN_REVIEW → APPROVED → PUBLISHED)
  - WorkflowAction : 상태 전이를 유발하는 액션 이름
  - WorkflowRole   : 워크플로 권한 판단에 사용하는 역할

설계 원칙:
  - 상태(Status)와 액션(Action)은 분리한다. 액션이 상태를 결정한다.
  - 소문자 값으로 DB에 저장 (기존 Phase 4 status 컨벤션 유지).
  - WorkflowRole은 Phase 2 ACL 역할 체계와 향후 연결 예정.
"""

from enum import Enum


class WorkflowStatus(str, Enum):
    """버전 워크플로 상태.

    전이 경로:
      DRAFT → IN_REVIEW → APPROVED → PUBLISHED → ARCHIVED
                       ↘ REJECTED → DRAFT
    """

    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    PUBLISHED = "published"
    REJECTED = "rejected"
    ARCHIVED = "archived"

    # Phase 4 호환 내부 상태 (워크플로 전이 대상 아님)
    SUPERSEDED = "superseded"
    DISCARDED = "discarded"


class WorkflowAction(str, Enum):
    """워크플로 액션.

    각 액션은 특정 상태 전이와 1:1 매핑된다.
    """

    SUBMIT_REVIEW = "submit_review"    # DRAFT → IN_REVIEW
    APPROVE = "approve"                # IN_REVIEW → APPROVED
    REJECT = "reject"                  # IN_REVIEW → REJECTED
    PUBLISH = "publish"                # APPROVED → PUBLISHED
    ARCHIVE = "archive"                # PUBLISHED → ARCHIVED
    RETURN_TO_DRAFT = "return_to_draft"  # REJECTED → DRAFT


class WorkflowRole(str, Enum):
    """워크플로 역할.

    권한 매핑:
      AUTHOR   — Draft 작성, Review 요청, 반려 후 재작업
      REVIEWER — 검토, 반려
      APPROVER — 승인, 게시
      ADMIN    — 모든 전이 허용
    """

    AUTHOR = "author"
    REVIEWER = "reviewer"
    APPROVER = "approver"
    ADMIN = "admin"
