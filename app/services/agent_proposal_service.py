"""
AgentProposalService — FG5.1 에이전트 Draft 제안 / 워크플로 전이 제안 비즈니스 로직.

책임:
  - propose_draft      : 에이전트가 proposed 상태 Draft를 생성
  - propose_transition : 에이전트가 워크플로 전이를 제안 (transition_proposals 큐에 등록)
  - approve_draft      : 인간 검토자가 proposed Draft를 승인 (→ approved)
  - reject_draft       : 인간 검토자가 proposed Draft를 반려 (→ rejected)
  - withdraw_proposal  : 에이전트가 제안을 회수 (→ withdrawn)
  - _create_mcp_task   : MCP Tasks 비동기 승인 플로우 태스크 생성 (self-hosted fallback)

설계 원칙 (FG5.1):
  - 에이전트 생성 Draft는 항상 workflow_status = 'proposed'로만 저장
  - 승인/반려는 문서 소유자 또는 reviewer/approver 역할 필수
  - 모든 상태 전이는 audit_events에 actor_type=agent 또는 actor_type=user로 기록
  - acting_on_behalf_of: 에이전트가 위임받은 user_id (있으면 기록)
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Optional

import psycopg2.extensions

from app.api.errors.exceptions import (
    ApiConflictError,
    ApiNotFoundError,
    ApiPermissionDeniedError,
)
from app.audit.emitter import audit_emitter
from app.domain.workflow.enums import WorkflowStatus
from app.repositories.versions_repository import versions_repository
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

# 승인/반려 권한 역할
_REVIEWER_ROLES = frozenset({"REVIEWER", "APPROVER", "ORG_ADMIN", "SUPER_ADMIN"})

# proposed → approved/rejected 허용 이전 상태
_APPROVABLE_STATUS = WorkflowStatus.PROPOSED.value


class AgentProposalService:
    """에이전트 제안 관련 비즈니스 로직."""

    # ------------------------------------------------------------------
    # 에이전트 제안 Draft 생성
    # ------------------------------------------------------------------

    def propose_draft(
        self,
        conn: psycopg2.extensions.connection,
        *,
        agent_id: str,
        acting_on_behalf_of: Optional[str],
        document_id: Optional[str],
        document_type_id: Optional[str],
        title: Optional[str],
        content: str,
        metadata: dict[str, Any],
        reason: str,
        request_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """에이전트가 proposed 상태의 Draft를 생성한다.

        새 문서(document_id=None) 또는 기존 문서(document_id 지정) 모두 지원.
        생성된 Version의 workflow_status는 항상 'proposed'.
        """
        self._assert_agent_active(conn, agent_id)

        # 새 문서 생성인 경우 document 레코드 먼저 생성
        if document_id is None:
            if not document_type_id:
                raise ApiConflictError("새 문서 생성 시 document_type_id가 필요합니다.")
            doc_title = title or "에이전트 제안 문서"
            document_id = self._create_document(
                conn,
                title=doc_title,
                document_type=document_type_id,
                created_by=acting_on_behalf_of or agent_id,
                metadata=metadata,
            )
        else:
            # 기존 문서 존재 확인
            self._assert_document_exists(conn, document_id)

        # 다음 버전 번호 계산
        version_number = versions_repository.get_next_version_number(conn, document_id)

        # Phase 1 FG 1-1: content_snapshot 은 ProseMirror doc 표준 포맷이어야 한다.
        # 과거 ``{type:"text", content: ...}`` 는 비표준이라 schemas/versions.py
        # validator 를 통과하지 못한다. snapshot_sync_service 로 표준 변환.
        from app.services.snapshot_sync_service import prosemirror_from_text
        content_snapshot = prosemirror_from_text(content)

        # Version 생성 (workflow_status = proposed)
        version_id = str(uuid.uuid4())
        now = utcnow()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO versions (
                    id, document_id, version_number, label, status,
                    workflow_status, change_summary, source, metadata,
                    title_snapshot, content_snapshot, created_by, created_at
                ) VALUES (
                    %s, %s, %s, %s, 'draft',
                    'proposed', %s, 'agent', %s,
                    %s, %s, %s, %s
                )
                """,
                (
                    version_id,
                    document_id,
                    version_number,
                    f"에이전트 제안 v{version_number}",
                    reason[:500] if reason else None,
                    json.dumps(metadata),
                    title,
                    json.dumps(content_snapshot),
                    acting_on_behalf_of or agent_id,
                    now,
                ),
            )

        # Phase 1 FG 1-1: content_snapshot 단일 정본 정책 상 nodes 테이블도
        # 즉시 파생 동기화한다. (INSERT 직후 FK 유효성 확보 + render_service /
        # vectorization_service 가 동일 데이터 관측)
        from app.services.snapshot_sync_service import (
            rebuild_annotation_anchoring,
            rebuild_nodes_from_snapshot,
            rebuild_tags_for_document,
        )
        rebuild_nodes_from_snapshot(conn, version_id, content_snapshot)
        # S3 Phase 2 FG 2-2 (2026-04-24): 태그 파생 동기화.
        # agent 제안도 본문에 hashtag / frontmatter 태그가 포함되면 자동 반영.
        rebuild_tags_for_document(
            conn,
            document_id=document_id,
            snapshot=content_snapshot,
            metadata=metadata,
        )
        # S3 Phase 3 FG 3-3 (2026-04-27): annotation anchoring 재계산.
        rebuild_annotation_anchoring(
            conn, document_id=document_id, snapshot=content_snapshot,
        )

        # MCP Task 생성 (비동기 승인 플로우)
        mcp_task_id = self._create_mcp_task(
            conn,
            title=f"Agent Proposal Review: {title or document_id}",
            description=f"에이전트 {agent_id}가 제안한 Draft",
            reference_type="version",
            reference_id=version_id,
            agent_id=agent_id,
        )

        # agent_proposals 통합 큐에 등록 (FG5.2)
        proposal_queue_id = str(uuid.uuid4())
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_proposals
                    (id, agent_id, proposal_type, reference_id, status, created_at, updated_at)
                VALUES (%s, %s, 'draft', %s, 'pending', %s, %s)
                """,
                (proposal_queue_id, agent_id, version_id, now, now),
            )

        # 감사 로그
        audit_emitter.emit(
            event_type="agent.draft.proposed",
            action="agent.propose_draft",
            actor_id=agent_id,
            actor_type="agent",
            resource_type="version",
            resource_id=version_id,
            result="success",
            request_id=request_id,
            previous_state=None,
            new_state="proposed",
            metadata={
                "document_id": document_id,
                "agent_id": agent_id,
                "acting_on_behalf_of": acting_on_behalf_of,
                "reason": reason,
                "mcp_task_id": mcp_task_id,
                "proposal_queue_id": proposal_queue_id,
            },
        )
        self._record_acting_on_behalf_of(conn, agent_id=agent_id, acting_on_behalf_of=acting_on_behalf_of)

        return {
            "draft_id": version_id,
            "status": "proposed",
            "created_by_agent": True,
            "created_at": now,
            "document_id": document_id,
            "version_id": version_id,
            "proposal_url": f"/documents/{document_id}/versions/{version_id}",
            "mcp_task_id": mcp_task_id,
        }

    # ------------------------------------------------------------------
    # 워크플로 전이 제안
    # ------------------------------------------------------------------

    def propose_transition(
        self,
        conn: psycopg2.extensions.connection,
        *,
        agent_id: str,
        acting_on_behalf_of: Optional[str],
        document_id: str,
        target_state: str,
        reason: str,
        approver_notes: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """에이전트가 문서의 워크플로 전이를 제안한다."""
        self._assert_agent_active(conn, agent_id)
        self._assert_document_exists(conn, document_id)

        # 현재 문서의 workflow_status 조회
        current_state = self._get_current_workflow_status(conn, document_id)

        proposal_id = str(uuid.uuid4())
        now = utcnow()

        # transition_proposals 등록
        with conn.cursor() as cur:
            # 최신 버전 id 조회
            cur.execute(
                """
                SELECT id FROM versions
                WHERE document_id = %s
                ORDER BY version_number DESC
                LIMIT 1
                """,
                (document_id,),
            )
            row = cur.fetchone()
            version_id = str(row["id"]) if row else None
            if not version_id:
                raise ApiNotFoundError(f"문서 {document_id}에 버전이 없습니다.")

            cur.execute(
                """
                INSERT INTO transition_proposals (
                    id, agent_id, document_id, version_id,
                    current_state, proposed_state, status,
                    reason, approver_notes, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 'pending_approval', %s, %s, %s, %s)
                """,
                (
                    proposal_id, agent_id, document_id, version_id,
                    current_state, target_state,
                    reason, approver_notes, now, now,
                ),
            )

        # MCP Task 생성
        mcp_task_id = self._create_mcp_task(
            conn,
            title=f"Agent Transition Proposal: {current_state} → {target_state}",
            description=f"에이전트 {agent_id}가 {document_id} 문서의 상태 전이를 제안",
            reference_type="transition_proposal",
            reference_id=proposal_id,
            agent_id=agent_id,
        )

        # mcp_task_id 역기록
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE transition_proposals SET mcp_task_id = %s WHERE id = %s",
                (mcp_task_id, proposal_id),
            )

        # agent_proposals 통합 큐에 등록 (FG5.2)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_proposals
                    (id, agent_id, proposal_type, reference_id, status, created_at, updated_at)
                VALUES (gen_random_uuid(), %s, 'transition', %s::uuid, 'pending', %s, %s)
                """,
                (agent_id, proposal_id, now, now),
            )

        audit_emitter.emit(
            event_type="agent.transition.proposed",
            action="agent.propose_transition",
            actor_id=agent_id,
            actor_type="agent",
            resource_type="document",
            resource_id=document_id,
            result="success",
            request_id=request_id,
            previous_state=current_state,
            new_state=f"pending:{target_state}",
            metadata={
                "document_id": document_id,
                "agent_id": agent_id,
                "acting_on_behalf_of": acting_on_behalf_of,
                "proposal_id": proposal_id,
                "target_state": target_state,
                "reason": reason,
            },
        )

        return {
            "transition_proposal_id": proposal_id,
            "document_id": document_id,
            "current_state": current_state,
            "proposed_state": target_state,
            "status": "pending_approval",
            "created_at": now,
            "mcp_task_id": mcp_task_id,
        }

    # ------------------------------------------------------------------
    # Draft 승인 (인간)
    # ------------------------------------------------------------------

    def approve_draft(
        self,
        conn: psycopg2.extensions.connection,
        *,
        draft_id: str,
        reviewer_id: str,
        reviewer_role: Optional[str],
        notes: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """인간 검토자가 proposed Draft를 승인한다 (→ approved)."""
        version, document_id = self._get_proposed_version(conn, draft_id)

        if version["workflow_status"] != _APPROVABLE_STATUS:
            raise ApiConflictError(
                f"Draft {draft_id}의 현재 상태({version['workflow_status']})는 "
                "승인할 수 없습니다. proposed 상태만 승인 가능합니다."
            )

        if reviewer_role not in _REVIEWER_ROLES:
            raise ApiPermissionDeniedError("Draft 승인은 REVIEWER/APPROVER/ADMIN 역할이 필요합니다.")

        now = utcnow()

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE versions SET workflow_status = 'approved' WHERE id = %s",
                (draft_id,),
            )
            # agent_proposals 큐 상태 동기화
            cur.execute(
                """
                UPDATE agent_proposals
                SET status = 'approved', reviewed_by = %s, review_notes = %s, review_timestamp = %s, updated_at = %s
                WHERE reference_id = %s::uuid AND proposal_type = 'draft' AND status = 'pending'
                """,
                (reviewer_id, notes, now, now, draft_id),
            )

        # MCP Task 상태 동기화 (completed)
        self._sync_mcp_task_state(conn, reference_type="version", reference_id=draft_id, new_state="completed")

        audit_emitter.emit(
            event_type="agent.draft.approved",
            action="draft.approve",
            actor_id=reviewer_id,
            actor_type="user",
            resource_type="version",
            resource_id=draft_id,
            result="success",
            request_id=request_id,
            previous_state="proposed",
            new_state="approved",
            metadata={
                "document_id": document_id,
                "notes": notes,
                "reviewer_role": reviewer_role,
            },
        )

        return {
            "draft_id": draft_id,
            "document_id": document_id,
            "previous_status": "proposed",
            "new_status": "approved",
            "reviewed_by": reviewer_id,
            "reviewed_at": now,
        }

    # ------------------------------------------------------------------
    # Draft 반려 (인간)
    # ------------------------------------------------------------------

    def reject_draft(
        self,
        conn: psycopg2.extensions.connection,
        *,
        draft_id: str,
        reviewer_id: str,
        reviewer_role: Optional[str],
        reason: str,
        request_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """인간 검토자가 proposed Draft를 반려한다 (→ rejected)."""
        version, document_id = self._get_proposed_version(conn, draft_id)

        if version["workflow_status"] != _APPROVABLE_STATUS:
            raise ApiConflictError(
                f"Draft {draft_id}의 현재 상태({version['workflow_status']})는 "
                "반려할 수 없습니다. proposed 상태만 반려 가능합니다."
            )

        if reviewer_role not in _REVIEWER_ROLES:
            raise ApiPermissionDeniedError("Draft 반려는 REVIEWER/APPROVER/ADMIN 역할이 필요합니다.")

        now = utcnow()

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE versions SET workflow_status = 'rejected' WHERE id = %s",
                (draft_id,),
            )
            # agent_proposals 큐 상태 동기화
            cur.execute(
                """
                UPDATE agent_proposals
                SET status = 'rejected', reviewed_by = %s, review_notes = %s, review_timestamp = %s, updated_at = %s
                WHERE reference_id = %s::uuid AND proposal_type = 'draft' AND status = 'pending'
                """,
                (reviewer_id, reason, now, now, draft_id),
            )

        # MCP Task 상태 동기화 (failed)
        self._sync_mcp_task_state(conn, reference_type="version", reference_id=draft_id, new_state="failed")

        audit_emitter.emit(
            event_type="agent.draft.rejected",
            action="draft.reject",
            actor_id=reviewer_id,
            actor_type="user",
            resource_type="version",
            resource_id=draft_id,
            result="success",
            request_id=request_id,
            previous_state="proposed",
            new_state="rejected",
            metadata={
                "document_id": document_id,
                "reason": reason,
                "reviewer_role": reviewer_role,
            },
        )

        return {
            "draft_id": draft_id,
            "document_id": document_id,
            "previous_status": "proposed",
            "new_status": "rejected",
            "reviewed_by": reviewer_id,
            "reviewed_at": now,
            "reason": reason,
        }

    # ------------------------------------------------------------------
    # 제안 회수 (에이전트)
    # ------------------------------------------------------------------

    def withdraw_proposal(
        self,
        conn: psycopg2.extensions.connection,
        *,
        agent_id: str,
        proposal_id: str,
        reason: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """에이전트가 자신의 proposed Draft 제안을 회수한다 (→ withdrawn).

        proposal_id는 propose_draft 응답의 draft_id(= version_id)를 사용.
        """
        self._assert_agent_active(conn, agent_id)

        # Draft 확인 및 소유 검증
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.id, v.document_id, v.workflow_status, v.created_by
                FROM versions v
                WHERE v.id = %s
                """,
                (proposal_id,),
            )
            row = cur.fetchone()

        if not row:
            raise ApiNotFoundError(f"제안 {proposal_id}을 찾을 수 없습니다.")

        if row["workflow_status"] != WorkflowStatus.PROPOSED.value:
            raise ApiConflictError(
                f"현재 상태({row['workflow_status']})는 회수할 수 없습니다. "
                "proposed 상태만 회수 가능합니다."
            )

        now = utcnow()
        document_id = str(row["document_id"])

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE versions SET workflow_status = 'withdrawn' WHERE id = %s",
                (proposal_id,),
            )
            # agent_proposals 큐 상태 동기화
            cur.execute(
                """
                UPDATE agent_proposals
                SET status = 'withdrawn', updated_at = %s
                WHERE reference_id = %s::uuid AND proposal_type = 'draft' AND status = 'pending'
                """,
                (now, proposal_id),
            )

        # MCP Task 상태 동기화 (failed — 에이전트 회수)
        self._sync_mcp_task_state(conn, reference_type="version", reference_id=proposal_id, new_state="failed")

        audit_emitter.emit(
            event_type="agent.draft.withdrawn",
            action="agent.withdraw_proposal",
            actor_id=agent_id,
            actor_type="agent",
            resource_type="version",
            resource_id=proposal_id,
            result="success",
            request_id=request_id,
            previous_state="proposed",
            new_state="withdrawn",
            metadata={
                "document_id": document_id,
                "agent_id": agent_id,
                "reason": reason,
            },
        )

        return {
            "proposal_id": proposal_id,
            "draft_id": proposal_id,
            "previous_status": "proposed",
            "new_status": "withdrawn",
            "withdrawn_at": now,
        }

    # ------------------------------------------------------------------
    # Batch 롤백 (undo approve/reject)
    # ------------------------------------------------------------------

    def batch_rollback(
        self,
        conn: psycopg2.extensions.connection,
        *,
        proposal_ids: list[str],
        original_action: str,
        reviewer_id: str,
        reviewer_role: Optional[str],
        actor_type: str = "user",
        request_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """승인/반려된 제안을 pending 상태로 되돌린다 (Undo).

        original_action: "approve" | "reject"
        committed 상태 버전이 참조된 제안은 롤백 불가(skipped).
        """
        if reviewer_role not in _REVIEWER_ROLES:
            raise ApiPermissionDeniedError("롤백은 REVIEWER/APPROVER/ADMIN 역할이 필요합니다.")

        rollback_from = "approved" if original_action == "approve" else "rejected"
        now = utcnow()

        rolled_back: list[str] = []
        skipped: list[str] = []

        for pid in proposal_ids:
            with conn.cursor() as cur:
                # FOR UPDATE OF ap: 행 잠금으로 동시 상태 변경 방지 (VULN-P7-001)
                cur.execute(
                    """
                    SELECT ap.id, ap.status, ap.reference_id, ap.proposal_type,
                           v.workflow_status
                    FROM agent_proposals ap
                    LEFT JOIN versions v ON v.id = ap.reference_id AND ap.proposal_type = 'draft'
                    WHERE ap.id = %s::uuid
                    FOR UPDATE OF ap
                    """,
                    (pid,),
                )
                row = cur.fetchone()

                if not row or row["status"] != rollback_from:
                    skipped.append(pid)
                    continue

                if row.get("workflow_status") == "committed":
                    skipped.append(pid)
                    continue

                cur.execute(
                    """
                    UPDATE agent_proposals
                    SET status = 'pending', reviewed_by = NULL, review_notes = NULL,
                        review_timestamp = NULL, updated_at = %s
                    WHERE id = %s::uuid
                    """,
                    (now, pid),
                )
                if row["proposal_type"] == "draft" and row.get("reference_id"):
                    cur.execute(
                        "UPDATE versions SET workflow_status = 'proposed' WHERE id = %s",
                        (str(row["reference_id"]),),
                    )

            audit_emitter.emit(
                event_type="agent.proposal.rolled_back",
                action="admin.batch_rollback",
                actor_id=reviewer_id,
                actor_type=actor_type,
                resource_type="agent_proposal",
                resource_id=pid,
                result="success",
                request_id=request_id,
                previous_state=rollback_from,
                new_state="pending",
                metadata={"original_action": original_action},
            )
            rolled_back.append(pid)

        return {
            "rolled_back": len(rolled_back),
            "skipped": len(skipped),
            "skipped_ids": skipped,
        }

    # ------------------------------------------------------------------
    # MCP Task 생성 (self-hosted fallback)
    # ------------------------------------------------------------------

    def _create_mcp_task(
        self,
        conn: psycopg2.extensions.connection,
        *,
        title: str,
        description: Optional[str],
        reference_type: str,
        reference_id: str,
        agent_id: Optional[str] = None,
    ) -> str:
        """mcp_tasks 테이블에 Task 레코드를 생성하고 task_id를 반환한다."""
        task_id = str(uuid.uuid4())
        now = utcnow()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mcp_tasks (
                    id, title, description, task_type, state,
                    reference_type, reference_id, agent_id,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, 'agent_proposal_review', 'input_required',
                          %s, %s, %s, %s, %s)
                """,
                (
                    task_id, title[:500], description,
                    reference_type, reference_id, agent_id, now, now,
                ),
            )
        return task_id

    def _sync_mcp_task_state(
        self,
        conn: psycopg2.extensions.connection,
        *,
        reference_type: str,
        reference_id: str,
        new_state: str,
    ) -> None:
        """Draft/Proposal 상태 변화를 MCP Task 상태에 동기화한다."""
        now = utcnow()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE mcp_tasks
                SET state = %s, progress = %s, updated_at = %s
                WHERE reference_type = %s AND reference_id = %s::uuid
                  AND state = 'input_required'
                """,
                (
                    new_state,
                    100 if new_state in ("completed", "failed") else 50,
                    now,
                    reference_type,
                    reference_id,
                ),
            )

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _assert_agent_active(self, conn: psycopg2.extensions.connection, agent_id: str) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, is_disabled FROM agents WHERE id = %s",
                (agent_id,),
            )
            row = cur.fetchone()
        if not row:
            raise ApiNotFoundError(f"에이전트 {agent_id}를 찾을 수 없습니다.")
        if row["is_disabled"]:
            raise ApiPermissionDeniedError(f"에이전트 {agent_id}는 비활성화(Kill Switch) 상태입니다.")

    def _assert_document_exists(self, conn: psycopg2.extensions.connection, document_id: str) -> None:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM documents WHERE id = %s", (document_id,))
            if not cur.fetchone():
                raise ApiNotFoundError(f"문서 {document_id}를 찾을 수 없습니다.")

    def _create_document(
        self,
        conn: psycopg2.extensions.connection,
        *,
        title: str,
        document_type: str,
        created_by: str,
        metadata: dict[str, Any],
    ) -> str:
        doc_id = str(uuid.uuid4())
        now = utcnow()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents (id, title, document_type, status, metadata, created_by, created_at, updated_at)
                VALUES (%s, %s, %s, 'draft', %s, %s, %s, %s)
                """,
                (doc_id, title, document_type, json.dumps(metadata), created_by, now, now),
            )
        return doc_id

    def _get_current_workflow_status(
        self, conn: psycopg2.extensions.connection, document_id: str
    ) -> str:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT workflow_status, status
                FROM versions
                WHERE document_id = %s
                ORDER BY version_number DESC
                LIMIT 1
                """,
                (document_id,),
            )
            row = cur.fetchone()
        if not row:
            return "draft"
        return row["workflow_status"] or row["status"] or "draft"

    def _get_proposed_version(
        self, conn: psycopg2.extensions.connection, draft_id: str
    ) -> tuple[dict, str]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, document_id, workflow_status, status FROM versions WHERE id = %s",
                (draft_id,),
            )
            row = cur.fetchone()
        if not row:
            raise ApiNotFoundError(f"Draft {draft_id}를 찾을 수 없습니다.")
        return dict(row), str(row["document_id"])

    def _record_acting_on_behalf_of(
        self,
        conn: psycopg2.extensions.connection,
        *,
        agent_id: str,
        acting_on_behalf_of: Optional[str],
    ) -> None:
        """audit_events 최신 행에 acting_on_behalf_of를 역기록한다 (best-effort)."""
        if not acting_on_behalf_of:
            return
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE audit_events
                    SET acting_on_behalf_of = %s
                    WHERE actor_user_id = %s
                      AND acting_on_behalf_of IS NULL
                    ORDER BY occurred_at DESC
                    LIMIT 1
                    """,
                    (acting_on_behalf_of, agent_id),
                )
        except Exception as exc:
            logger.warning("acting_on_behalf_of 역기록 실패: %s", exc)


agent_proposal_service = AgentProposalService()
