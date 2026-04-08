"""
DraftService — Draft/Publish/Restore 핵심 비즈니스 로직.

책임:
  - save_draft: 문서의 Draft를 생성하거나 전체 교체한다 (PUT semantics).
  - discard_draft: 현재 활성 Draft를 폐기(삭제)한다.
  - publish: 현재 Draft를 Published로 전환한다.
  - restore: 과거 버전을 기준으로 새 Draft를 생성한다.
  - list_versions: 버전 목록을 is_current_* 플래그와 함께 반환한다.
  - get_version_detail: 버전 상세를 actions 정보와 함께 반환한다.

설계 원칙 (Task 4-5):
  - 문서당 활성 Draft는 최대 1개.
  - Publish = Draft 상태 전이 (새 버전 생성 아님).
  - Restore = 새 Draft 생성 (과거 버전 불변 유지).
  - 기존 Draft 존재 시 Restore 불가 (409).

권한 규칙 (Task 4-8):
  - save_draft / discard_draft: editor 이상
  - publish / restore: publisher 이상
  (실제 권한 체크는 router에서 수행, 서비스는 상태 정책만 담당)
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg2.extensions

from app.api.errors.exceptions import ApiConflictError, ApiNotFoundError, ApiVersionNotEditableError
from app.api.query.models import ParsedListQuery
from app.models.version import Version
from app.repositories.documents_repository import documents_repository
from app.repositories.versions_repository import versions_repository
from app.schemas.versions import (
    DraftNodeSaveRequest,
    DraftSaveRequest,
    PublishRequest,
    RestoreRequest,
    VersionActionsResponse,
    VersionDetailResponse,
    VersionResponse,
    VersionSummaryResponse,
)

logger = logging.getLogger(__name__)


def _can_restore(
    version: Version,
    document_current_draft_id: Optional[str],
    actor_role: Optional[str],
) -> tuple[bool, Optional[str]]:
    """복원 가능 여부와 불가 이유를 반환한다."""
    if version.status not in ("published", "superseded"):
        return False, "invalid_version_status"
    if document_current_draft_id is not None:
        return False, "active_draft_exists"
    if actor_role not in ("publisher", "admin"):
        return False, "insufficient_permission"
    return True, None


def _to_version_response(version: Version, workflow_status: Optional[str] = None) -> VersionResponse:
    return VersionResponse(
        id=version.id,
        document_id=version.document_id,
        version_number=version.version_number,
        label=version.label,
        status=version.status,
        workflow_status=workflow_status,
        change_summary=version.change_summary,
        source=version.source,
        metadata=version.metadata,
        created_by=version.created_by,
        created_at=version.created_at,
        parent_version_id=version.parent_version_id,
        restored_from_version_id=version.restored_from_version_id,
        title_snapshot=version.title_snapshot,
        summary_snapshot=version.summary_snapshot,
        published_by=version.published_by,
        published_at=version.published_at,
    )


def _to_summary_response(
    version: Version,
    *,
    current_draft_id: Optional[str],
    current_published_id: Optional[str],
    actor_role: Optional[str],
    workflow_status: Optional[str] = None,
) -> VersionSummaryResponse:
    is_current_draft = version.id == current_draft_id
    is_current_published = version.id == current_published_id
    can, _ = _can_restore(version, current_draft_id, actor_role)
    return VersionSummaryResponse(
        id=version.id,
        document_id=version.document_id,
        version_number=version.version_number,
        label=version.label,
        status=version.status,
        workflow_status=workflow_status,
        change_summary=version.change_summary,
        source=version.source,
        created_by=version.created_by,
        created_at=version.created_at,
        published_at=version.published_at,
        published_by=version.published_by,
        restored_from_version_id=version.restored_from_version_id,
        is_current_draft=is_current_draft,
        is_current_published=is_current_published,
        can_restore=can,
    )


class DraftService:
    """Draft/Publish/Restore 비즈니스 로직 서비스."""

    # ------------------------------------------------------------------
    # save_draft
    # ------------------------------------------------------------------

    def save_draft(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        request: DraftSaveRequest,
        *,
        actor_id: Optional[str] = None,
    ) -> VersionResponse:
        """현재 Draft를 교체하거나 새 Draft를 생성한다 (PUT semantics).

        흐름:
          1. 문서 존재 검증
          2. 현재 활성 Draft 확인
          3. Draft 없음 → 새 Draft 생성 + Document.current_draft_version_id 갱신
          4. Draft 있음 → 기존 Draft content 교체 (version_number 유지)
        """
        doc = documents_repository.get_by_id(conn, document_id)
        if doc is None:
            raise ApiNotFoundError(f"Document '{document_id}' not found")

        title_snap = request.title or doc.title
        summary_snap = request.summary if request.summary is not None else doc.summary

        existing_draft = None
        if doc.current_draft_version_id:
            existing_draft = versions_repository.get_by_id(
                conn, doc.current_draft_version_id
            )

        # Phase 5 정책 (Task 5-9 §3, §4): 편집 불가 상태 검사
        # IN_REVIEW / APPROVED / PUBLISHED 상태의 버전은 직접 수정 금지.
        # 수정하려면 REJECTED → DRAFT 복귀 후 수정해야 한다.
        if existing_draft is not None:
            from app.repositories.workflow_repository import workflow_repository
            from app.domain.workflow.policies import EDITABLE_STATUSES
            from app.domain.workflow.enums import WorkflowStatus
            raw_wf = workflow_repository.get_workflow_status(conn, existing_draft.id)
            wf_status_str = raw_wf if raw_wf else existing_draft.status
            try:
                wf_status = WorkflowStatus(wf_status_str)
            except ValueError:
                wf_status = WorkflowStatus.DRAFT
            if wf_status not in EDITABLE_STATUSES:
                raise ApiVersionNotEditableError(
                    f"Cannot edit version in '{wf_status.value}' state. "
                    "Return to draft first.",
                    details={"workflow_status": wf_status.value},
                )

        if existing_draft is None:
            # 새 Draft 생성
            next_number = versions_repository.get_next_version_number(conn, document_id)
            current_published = (
                versions_repository.get_by_id(conn, doc.current_published_version_id)
                if doc.current_published_version_id else None
            )
            version = versions_repository.create(
                conn,
                document_id=document_id,
                version_number=next_number,
                label=request.label,
                status="draft",
                change_summary=request.change_summary,
                source="manual",
                metadata={},
                created_by=actor_id,
                parent_version_id=current_published.id if current_published else None,
                title_snapshot=title_snap,
                summary_snapshot=summary_snap,
                metadata_snapshot=doc.metadata,
                content_snapshot=request.content_snapshot,
            )
            documents_repository.update_version_pointers(
                conn,
                document_id,
                current_draft_version_id=version.id,
                updated_by=actor_id,
            )
            logger.info(
                "Draft created: doc=%s ver=%s v%d actor=%s",
                document_id, version.id, next_number, actor_id,
            )
        else:
            # 기존 Draft 내용 교체
            version = versions_repository.update_content(
                conn,
                existing_draft.id,
                label=request.label,
                change_summary=request.change_summary,
                title_snapshot=title_snap,
                summary_snapshot=summary_snap,
                metadata_snapshot=doc.metadata,
                content_snapshot=request.content_snapshot,
            )
            logger.info(
                "Draft updated: doc=%s ver=%s v%d actor=%s",
                document_id, version.id, version.version_number, actor_id,
            )

        # documents.title 동기화 — 에디터에서 제목 변경 시 목록 뷰에도 즉시 반영
        if request.title and request.title != doc.title:
            documents_repository.update(
                conn,
                document_id,
                title=request.title,
                updated_by=actor_id,
            )

        from app.repositories.workflow_repository import workflow_repository
        wf_status = workflow_repository.get_workflow_status(conn, version.id) or version.status
        return _to_version_response(version, workflow_status=wf_status)

    # ------------------------------------------------------------------
    # discard_draft
    # ------------------------------------------------------------------

    def discard_draft(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        *,
        actor_id: Optional[str] = None,
    ) -> None:
        """현재 활성 Draft를 폐기한다.

        - current_draft_version_id 포인터를 NULL로 초기화한다.
        - 버전 row는 status='discarded'로 업데이트한다 (이력 보존).
        """
        doc = documents_repository.get_by_id(conn, document_id)
        if doc is None:
            raise ApiNotFoundError(f"Document '{document_id}' not found")

        if doc.current_draft_version_id is None:
            raise ApiConflictError(
                "No active draft to discard",
                details="no_active_draft",
            )

        # 포인터 먼저 해제 (FK 제약 우회)
        documents_repository.update_version_pointers(
            conn, document_id, clear_draft=True, updated_by=actor_id
        )

        # Draft 버전 status → discarded
        versions_repository.update_status(
            conn, doc.current_draft_version_id, status="discarded"
        )

        logger.info(
            "Draft discarded: doc=%s ver=%s actor=%s",
            document_id, doc.current_draft_version_id, actor_id,
        )

    # ------------------------------------------------------------------
    # publish
    # ------------------------------------------------------------------

    def publish(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        request: PublishRequest,
        *,
        actor_id: Optional[str] = None,
    ) -> VersionResponse:
        """현재 Draft를 Published 상태로 전환한다.

        흐름:
          1. 활성 Draft 존재 확인
          2. 기존 current_published → superseded
          3. Draft → published (published_by/at 기록)
          4. Document 포인터 갱신 (current_published=new, clear draft)
        """
        doc = documents_repository.get_by_id(conn, document_id)
        if doc is None:
            raise ApiNotFoundError(f"Document '{document_id}' not found")

        if doc.current_draft_version_id is None:
            raise ApiConflictError(
                "No active draft to publish",
                details="no_active_draft",
            )

        draft = versions_repository.get_by_id(conn, doc.current_draft_version_id)
        if draft is None or draft.status != "draft":
            raise ApiConflictError(
                "Current draft version is not in draft status",
                details="invalid_draft_status",
            )

        now = datetime.now(timezone.utc)

        # 기존 published → superseded
        if doc.current_published_version_id:
            versions_repository.update_status(
                conn, doc.current_published_version_id, status="superseded"
            )

        # change_summary 덮어쓰기 (요청에 제공된 경우)
        if request.change_summary is not None:
            versions_repository.update_content(
                conn, draft.id, change_summary=request.change_summary
            )

        # Draft → published
        published_version = versions_repository.update_status(
            conn,
            draft.id,
            status="published",
            published_by=actor_id,
            published_at=now,
        )

        # Document 포인터 갱신
        documents_repository.update_version_pointers(
            conn,
            document_id,
            current_published_version_id=published_version.id,
            clear_draft=True,
            updated_by=actor_id,
        )

        logger.info(
            "Document published: doc=%s ver=%s v%d actor=%s",
            document_id, published_version.id, published_version.version_number, actor_id,
        )
        from app.repositories.workflow_repository import workflow_repository
        wf_status = workflow_repository.get_workflow_status(conn, published_version.id) or published_version.status
        return _to_version_response(published_version, workflow_status=wf_status)

    # ------------------------------------------------------------------
    # restore
    # ------------------------------------------------------------------

    def restore(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        version_id: str,
        request: RestoreRequest,
        *,
        actor_id: Optional[str] = None,
        actor_role: Optional[str] = None,
    ) -> VersionResponse:
        """과거 버전을 기준으로 새 Draft를 생성한다.

        흐름:
          1. 대상 버전 존재 및 소속 확인
          2. 복원 가능 상태 검증 (published/superseded만 허용)
          3. 기존 Draft 존재 시 409 반환
          4. 새 Draft 생성 (restored_from_version_id 기록)
          5. Document.current_draft_version_id 갱신
        """
        doc = documents_repository.get_by_id(conn, document_id)
        if doc is None:
            raise ApiNotFoundError(f"Document '{document_id}' not found")

        target = versions_repository.get_by_document_and_version_id(
            conn, document_id, version_id
        )
        if target is None:
            raise ApiNotFoundError(
                f"Version '{version_id}' not found in document '{document_id}'"
            )

        can, reason = _can_restore(target, doc.current_draft_version_id, actor_role)
        if not can:
            if reason == "active_draft_exists":
                raise ApiConflictError(
                    "Cannot restore: an active draft already exists. Discard it first.",
                    details="active_draft_exists",
                )
            if reason == "invalid_version_status":
                raise ApiConflictError(
                    f"Cannot restore version with status '{target.status}'. "
                    "Only published or superseded versions can be restored.",
                    details="invalid_version_status",
                )
            # insufficient_permission은 router에서 먼저 차단하지만 방어적 처리
            raise ApiConflictError(
                "Insufficient permission to restore",
                details="insufficient_permission",
            )

        next_number = versions_repository.get_next_version_number(conn, document_id)

        new_draft = versions_repository.create(
            conn,
            document_id=document_id,
            version_number=next_number,
            label=None,
            status="draft",
            change_summary=request.change_summary,
            source="restore",
            metadata={},
            created_by=actor_id,
            parent_version_id=doc.current_published_version_id,
            restored_from_version_id=target.id,
            title_snapshot=target.title_snapshot,
            summary_snapshot=target.summary_snapshot,
            metadata_snapshot=target.metadata_snapshot,
            content_snapshot=target.content_snapshot,
        )

        documents_repository.update_version_pointers(
            conn,
            document_id,
            current_draft_version_id=new_draft.id,
            updated_by=actor_id,
        )

        logger.info(
            "Version restored: doc=%s from=%s new_ver=%s v%d actor=%s",
            document_id, target.id, new_draft.id, next_number, actor_id,
        )
        from app.repositories.workflow_repository import workflow_repository
        wf_status = workflow_repository.get_workflow_status(conn, new_draft.id) or new_draft.status
        return _to_version_response(new_draft, workflow_status=wf_status)

    # ------------------------------------------------------------------
    # list_versions
    # ------------------------------------------------------------------

    def list_versions(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        query: ParsedListQuery,
        *,
        actor_role: Optional[str] = None,
    ) -> tuple[list[VersionSummaryResponse], int]:
        """버전 목록을 is_current_* 플래그 및 can_restore와 함께 반환한다."""
        doc = documents_repository.get_by_id(conn, document_id)
        if doc is None:
            raise ApiNotFoundError(f"Document '{document_id}' not found")

        sort_field = "version_number"
        sort_dir = "DESC"
        if query.sort_orders:
            first = query.sort_orders[0]
            sort_field = first.field
            sort_dir = "DESC" if first.direction == "desc" else "ASC"

        page = query.page if query.page else 1
        page_size = query.page_size if query.page_size else 20

        versions, total = versions_repository.list_by_document_id(
            conn, document_id,
            page=page, page_size=page_size,
            sort_field=sort_field, sort_dir=sort_dir,
        )

        # Phase 5: workflow_status 일괄 조회
        from app.repositories.workflow_repository import workflow_repository
        summaries = [
            _to_summary_response(
                v,
                current_draft_id=doc.current_draft_version_id,
                current_published_id=doc.current_published_version_id,
                actor_role=actor_role,
                workflow_status=workflow_repository.get_workflow_status(conn, v.id) or v.status,
            )
            for v in versions
        ]
        return summaries, total

    # ------------------------------------------------------------------
    # get_version_detail
    # ------------------------------------------------------------------

    def get_version_detail(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        version_id: str,
        *,
        actor_role: Optional[str] = None,
        include_content: bool = True,
    ) -> VersionDetailResponse:
        """버전 상세를 actions, is_current_* 플래그와 함께 반환한다."""
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

        can, reason = _can_restore(version, doc.current_draft_version_id, actor_role)

        next_vnum = (
            versions_repository.get_next_version_number(conn, document_id)
            if can else None
        )

        # Phase 5: workflow_status 조회
        from app.repositories.workflow_repository import workflow_repository
        workflow_status = workflow_repository.get_workflow_status(conn, version_id) or version.status

        return VersionDetailResponse(
            id=version.id,
            document_id=version.document_id,
            version_number=version.version_number,
            label=version.label,
            status=version.status,
            workflow_status=workflow_status,
            change_summary=version.change_summary,
            source=version.source,
            metadata=version.metadata,
            created_by=version.created_by,
            created_at=version.created_at,
            parent_version_id=version.parent_version_id,
            restored_from_version_id=version.restored_from_version_id,
            title_snapshot=version.title_snapshot,
            summary_snapshot=version.summary_snapshot,
            metadata_snapshot=version.metadata_snapshot,
            content_snapshot=version.content_snapshot if include_content else None,
            published_by=version.published_by,
            published_at=version.published_at,
            is_current_draft=version.id == doc.current_draft_version_id,
            is_current_published=version.id == doc.current_published_version_id,
            actions=VersionActionsResponse(
                can_restore=can,
                restore_blocked_reason=reason,
            ),
        )

    # ------------------------------------------------------------------
    # save_draft_nodes
    # ------------------------------------------------------------------

    def save_draft_nodes(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        version_id: str,
        request: DraftNodeSaveRequest,
        *,
        actor_id: Optional[str] = None,
    ) -> VersionResponse:
        """에디터에서 전달한 노드 목록으로 Draft를 저장한다.

        PUT /draft (content_snapshot 기반) 와 달리, 노드를 nodes 테이블에 직접 저장한다.

        흐름:
          1. version_id가 document의 current_draft_version_id인지 검증
          2. 워크플로 상태 검사 (편집 불가 상태면 409)
          3. 노드 교체 (DELETE + INSERT)
          4. title_snapshot / summary_snapshot 갱신
          5. documents.title 동기화 (title 변경 시)
        """
        from app.repositories.nodes_repository import nodes_repository
        from app.repositories.workflow_repository import workflow_repository
        from app.domain.workflow.policies import EDITABLE_STATUSES
        from app.domain.workflow.enums import WorkflowStatus

        doc = documents_repository.get_by_id(conn, document_id)
        if doc is None:
            raise ApiNotFoundError(f"Document '{document_id}' not found")

        if doc.current_draft_version_id != version_id:
            raise ApiConflictError(
                "Version is not the current draft of this document",
                details={"current_draft_version_id": doc.current_draft_version_id},
            )

        draft = versions_repository.get_by_id(conn, version_id)
        if draft is None:
            raise ApiNotFoundError(f"Version '{version_id}' not found")

        # 워크플로 상태 검사
        raw_wf = workflow_repository.get_workflow_status(conn, version_id)
        wf_status_str = raw_wf if raw_wf else draft.status
        try:
            wf_status = WorkflowStatus(wf_status_str)
        except ValueError:
            wf_status = WorkflowStatus.DRAFT
        if wf_status not in EDITABLE_STATUSES:
            raise ApiVersionNotEditableError(
                f"Cannot edit version in '{wf_status.value}' state. Return to draft first.",
                details={"workflow_status": wf_status.value},
            )

        # 노드 교체 — frontend order → DB order_index 매핑
        node_items = [
            {
                "id": item.id,
                "node_type": item.node_type,
                "order_index": item.order,
                "parent_id": item.parent_id,
                "title": item.title,
                "content": item.content,
                "metadata": item.metadata,
            }
            for item in request.nodes
        ]
        nodes_repository.replace_for_version(conn, version_id, node_items)

        # title_snapshot / summary_snapshot 갱신
        title_snap = request.title if request.title is not None else (draft.title_snapshot or doc.title)
        summary_snap = request.summary if request.summary is not None else draft.summary_snapshot
        version = versions_repository.update_content(
            conn,
            version_id,
            label=request.label,
            change_summary=request.change_summary,
            title_snapshot=title_snap,
            summary_snapshot=summary_snap,
        )

        # documents.title 동기화
        if request.title and request.title != doc.title:
            documents_repository.update(conn, document_id, title=request.title, updated_by=actor_id)

        logger.info(
            "Draft nodes saved: doc=%s ver=%s nodes=%d actor=%s",
            document_id, version_id, len(node_items), actor_id,
        )

        wf_status_val = workflow_repository.get_workflow_status(conn, version.id) or version.status
        return _to_version_response(version, workflow_status=wf_status_val)


# 모듈 수준 싱글턴
draft_service = DraftService()
