"""
Conversations API 라우터 — Phase 3 S2.

엔드포인트:
  GET    /conversations                           대화 목록 (페이지네이션 + FTS)
  POST   /conversations                           새 대화 생성
  GET    /conversations/{id}                      대화 상세 (turns 포함)
  PUT    /conversations/{id}                      대화 수정 (제목/메타/상태)
  DELETE /conversations/{id}                      대화 삭제 (soft delete)
  GET    /conversations/{id}/turns                턴 목록
  GET    /conversations/{id}/turns/{turn_id}      특정 턴 상세
  POST   /conversations/{id}/turns/{turn_id}/redact  민감정보 제거

설계 원칙:
  - S2 원칙 ⑥: Scope Profile 기반 ACL — 코드에 scope 문자열 하드코딩 금지
  - S2 원칙 ⑥: 모든 쓰기 작업에 actor_type 감사 로그 기록
  - 응답: success_response() / paginated_list_response() helper 사용
  - 인증: resolve_current_actor 의존성 (기존 패턴 동일)
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.auth import resolve_current_actor
from app.api.auth.models import ActorContext, ActorType
from app.api.responses import list_response, success_response
from app.audit.emitter import audit_emitter
from app.db import get_db
from app.repositories.conversation_repository import (
    ConversationRepository,
    MessageRepository,
    TurnRepository,
)
from app.schemas.conversation import (
    ConversationCreateRequest,
    ConversationDetailOut,
    ConversationOut,
    ConversationUpdateRequest,
    MessageOut,
    RedactRequest,
    TurnOut,
)
from app.utils.http_errors import bad_request, conflict, not_found

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 헬퍼: Scope Profile 조회 (S2 원칙 ⑥)
# ---------------------------------------------------------------------------

def _resolve_scope_profile(actor: ActorContext) -> dict:
    """actor의 역할/타입에서 Scope Profile을 결정한다.

    S2 원칙 ⑥: 접근 범위는 Scope Profile로 관리.
    하드코딩된 scope 문자열 금지 — 값은 반드시 외부 입력(헤더/설정)에서 파생.

    현재 구현: actor.role 기반 기본 scope 반환.
    향후: system_settings 테이블의 scope_profile 설정으로 확장.
    """
    role = getattr(actor, "role", "") or ""
    role_upper = role.upper()

    # SUPER_ADMIN / ORG_ADMIN 은 조직 범위 조회 가능
    if role_upper in ("SUPER_ADMIN", "ORG_ADMIN"):
        return {"scope": "organization", "include_archived": True}
    # SERVICE actor (에이전트) 도 조직 범위
    if actor.actor_type == ActorType.SERVICE:
        return {"scope": "organization", "include_archived": False}
    # 일반 사용자: 자신의 대화만
    return {"scope": "private", "include_archived": False}


def _actor_type_str(actor: ActorContext) -> str:
    """ActorContext → 감사 로그용 actor_type 문자열."""
    raw = getattr(actor.actor_type, "value", str(actor.actor_type)).lower()
    return "agent" if raw in ("service", "agent") else "user"


def _domain_to_turn_out(turn, messages: list | None = None) -> TurnOut:
    msg_out = [
        MessageOut(
            id=m.id, turn_id=m.turn_id, role=m.role,
            content=m.content, created_at=m.created_at, metadata=m.metadata,
        )
        for m in (messages or [])
    ]
    return TurnOut(
        id=turn.id,
        conversation_id=turn.conversation_id,
        turn_number=turn.turn_number,
        created_at=turn.created_at,
        user_message=turn.user_message,
        assistant_response=turn.assistant_response,
        retrieval_metadata=turn.retrieval_metadata,
        messages=msg_out,
    )


def _domain_to_conv_out(conv) -> ConversationOut:
    return ConversationOut(
        id=conv.id,
        owner_id=conv.owner_id,
        organization_id=conv.organization_id,
        title=conv.title,
        status=conv.status,
        metadata=conv.metadata,
        retention_days=conv.retention_days,
        expires_at=conv.expires_at,
        access_level=conv.access_level,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
    )


# ---------------------------------------------------------------------------
# ACL 검사 헬퍼
# ---------------------------------------------------------------------------

def _assert_read_access(conv, actor: ActorContext) -> None:
    """대화 읽기 권한 검사.

    소유자이거나 access_level이 organization/public이면 허용.
    Scope Profile 기반 필터링은 Repository 레이어에서 적용됨.
    """
    if conv.owner_id == str(actor.actor_id):
        return
    if conv.access_level in ("organization", "public"):
        return
    raise HTTPException(status_code=403, detail="이 대화에 접근할 권한이 없습니다.")


def _assert_write_access(conv, actor: ActorContext) -> None:
    """대화 쓰기 권한 검사 — 소유자만 허용."""
    if conv.owner_id != str(actor.actor_id):
        role = getattr(actor, "role", "") or ""
        if role.upper() not in ("SUPER_ADMIN", "ORG_ADMIN"):
            raise HTTPException(status_code=403, detail="소유자만 수정할 수 있습니다.")


# ---------------------------------------------------------------------------
# GET /conversations
# ---------------------------------------------------------------------------

@router.get("", summary="대화 목록 조회")
def list_conversations(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: Optional[str] = Query(None),
    sort_by: str = Query("created_at"),
    sort_desc: bool = Query(True),
    q: Optional[str] = Query(None, description="전체 텍스트 검색어"),
    actor: ActorContext = Depends(resolve_current_actor),
):
    """사용자의 대화 목록 조회 (Scope Profile 기반 ACL + FTS 지원)."""
    ctx = request.state.context
    scope_profile = _resolve_scope_profile(actor)

    with get_db() as conn:
        repo = ConversationRepository(conn)
        org_id = getattr(actor, "tenant_id", None) or ""
        conversations, total = repo.list_by_owner(
            owner_id=str(actor.actor_id),
            organization_id=org_id,
            status=status,
            sort_by=sort_by,
            sort_desc=sort_desc,
            limit=limit,
            offset=offset,
            search_query=q,
        )

    items = [_domain_to_conv_out(c) for c in conversations]
    page = (offset // limit) + 1 if limit else 1
    return list_response(
        data=items,
        request_id=ctx.request_id,
        page=page,
        page_size=limit,
        total=total,
        has_next=(offset + limit) < total,
    )


# ---------------------------------------------------------------------------
# POST /conversations
# ---------------------------------------------------------------------------

@router.post("", summary="새 대화 생성", status_code=201)
def create_conversation(
    request: Request,
    body: ConversationCreateRequest,
    actor: ActorContext = Depends(resolve_current_actor),
):
    """새 Conversation 생성.

    - `retention_days` 미입력 시 기본값 90일 적용.
    - `access_level` 기본값: private.
    """
    ctx = request.state.context
    org_id = getattr(actor, "tenant_id", None) or ""
    actor_type_s = _actor_type_str(actor)

    with get_db() as conn:
        repo = ConversationRepository(conn)
        conv = repo.create(
            owner_id=str(actor.actor_id),
            organization_id=org_id,
            title=body.title,
            retention_days=body.retention_days or 90,
            metadata=body.metadata,
            access_level=body.access_level,
        )
        conn.commit()

    audit_emitter.emit(
        event_type="conversation.created",
        action="conversation.create",
        actor_id=str(actor.actor_id),
        actor_type=actor_type_s,
        resource_type="conversation",
        resource_id=conv.id,
        result="success",
        request_id=ctx.request_id,
    )

    return success_response(data=_domain_to_conv_out(conv), request_id=ctx.request_id)


# ---------------------------------------------------------------------------
# GET /conversations/{conversation_id}
# ---------------------------------------------------------------------------

@router.get("/{conversation_id}", summary="대화 상세 조회")
def get_conversation(
    request: Request,
    conversation_id: str,
    include_turns: bool = Query(True),
    actor: ActorContext = Depends(resolve_current_actor),
):
    """대화 상세 조회 (turns 포함 여부 선택)."""
    ctx = request.state.context

    with get_db() as conn:
        repo = ConversationRepository(conn)
        turn_repo = TurnRepository(conn)
        msg_repo = MessageRepository(conn)

        conv = repo.get_by_id(conversation_id)
        if not conv:
            raise not_found("대화를 찾을 수 없습니다.")

        _assert_read_access(conv, actor)

        turns_out: list[TurnOut] = []
        if include_turns:
            turns = turn_repo.list_by_conversation(conversation_id)
            for turn in turns:
                msgs = msg_repo.list_by_turn(turn.id)
                turns_out.append(_domain_to_turn_out(turn, msgs))

    detail = ConversationDetailOut(
        **_domain_to_conv_out(conv).model_dump(),
        turns=turns_out,
    )
    return success_response(data=detail, request_id=ctx.request_id)


# ---------------------------------------------------------------------------
# PUT /conversations/{conversation_id}
# ---------------------------------------------------------------------------

@router.put("/{conversation_id}", summary="대화 수정")
def update_conversation(
    request: Request,
    conversation_id: str,
    body: ConversationUpdateRequest,
    actor: ActorContext = Depends(resolve_current_actor),
):
    """대화 제목 / 메타데이터 / 상태 / 접근 레벨 수정."""
    ctx = request.state.context
    actor_type_s = _actor_type_str(actor)

    with get_db() as conn:
        repo = ConversationRepository(conn)
        conv = repo.get_by_id(conversation_id)
        if not conv:
            raise not_found("대화를 찾을 수 없습니다.")

        _assert_write_access(conv, actor)

        updated = repo.update(
            conversation_id,
            title=body.title,
            status=body.status,
            metadata=body.metadata,
        )
        if updated is None:
            raise not_found("수정 대상을 찾을 수 없습니다.")
        conn.commit()

    audit_emitter.emit(
        event_type="conversation.updated",
        action="conversation.update",
        actor_id=str(actor.actor_id),
        actor_type=actor_type_s,
        resource_type="conversation",
        resource_id=conversation_id,
        result="success",
        request_id=ctx.request_id,
    )
    return success_response(data=_domain_to_conv_out(updated), request_id=ctx.request_id)


# ---------------------------------------------------------------------------
# DELETE /conversations/{conversation_id}
# ---------------------------------------------------------------------------

@router.delete("/{conversation_id}", summary="대화 삭제 (soft delete)")
def delete_conversation(
    request: Request,
    conversation_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
):
    """대화 soft delete — deleted_at 설정, status='deleted'."""
    ctx = request.state.context
    actor_type_s = _actor_type_str(actor)

    with get_db() as conn:
        repo = ConversationRepository(conn)
        conv = repo.get_by_id(conversation_id)
        if not conv:
            raise not_found("대화를 찾을 수 없습니다.")

        _assert_write_access(conv, actor)

        ok = repo.soft_delete(conversation_id)
        if not ok:
            raise conflict("이미 삭제된 대화입니다.")
        conn.commit()

    audit_emitter.emit(
        event_type="conversation.deleted",
        action="conversation.delete",
        actor_id=str(actor.actor_id),
        actor_type=actor_type_s,
        resource_type="conversation",
        resource_id=conversation_id,
        result="success",
        request_id=ctx.request_id,
    )
    return success_response(data={"deleted": True}, request_id=ctx.request_id)


# ---------------------------------------------------------------------------
# GET /conversations/{conversation_id}/turns
# ---------------------------------------------------------------------------

@router.get("/{conversation_id}/turns", summary="턴 목록 조회")
def list_turns(
    request: Request,
    conversation_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
):
    """대화의 모든 턴 목록 조회 (turn_number 오름차순)."""
    ctx = request.state.context

    with get_db() as conn:
        repo = ConversationRepository(conn)
        turn_repo = TurnRepository(conn)
        msg_repo = MessageRepository(conn)

        conv = repo.get_by_id(conversation_id)
        if not conv:
            raise not_found("대화를 찾을 수 없습니다.")
        _assert_read_access(conv, actor)

        turns = turn_repo.list_by_conversation(conversation_id)
        turns_out = []
        for turn in turns:
            msgs = msg_repo.list_by_turn(turn.id)
            turns_out.append(_domain_to_turn_out(turn, msgs))

    return success_response(data=turns_out, request_id=ctx.request_id)


# ---------------------------------------------------------------------------
# GET /conversations/{conversation_id}/turns/{turn_id}
# ---------------------------------------------------------------------------

@router.get("/{conversation_id}/turns/{turn_id}", summary="특정 턴 상세 조회")
def get_turn(
    request: Request,
    conversation_id: str,
    turn_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
):
    """특정 턴 상세 조회 (메시지 포함)."""
    ctx = request.state.context

    with get_db() as conn:
        repo = ConversationRepository(conn)
        turn_repo = TurnRepository(conn)
        msg_repo = MessageRepository(conn)

        conv = repo.get_by_id(conversation_id)
        if not conv:
            raise not_found("대화를 찾을 수 없습니다.")
        _assert_read_access(conv, actor)

        turn = turn_repo.get_by_id(turn_id)
        if not turn or turn.conversation_id != conversation_id:
            raise not_found("턴을 찾을 수 없습니다.")

        msgs = msg_repo.list_by_turn(turn_id)
        turn_out = _domain_to_turn_out(turn, msgs)

    return success_response(data=turn_out, request_id=ctx.request_id)


# ---------------------------------------------------------------------------
# POST /conversations/{conversation_id}/turns/{turn_id}/redact
# ---------------------------------------------------------------------------

@router.post(
    "/{conversation_id}/turns/{turn_id}/redact",
    summary="민감 정보 제거 (redact)",
)
def redact_turn(
    request: Request,
    conversation_id: str,
    turn_id: str,
    body: RedactRequest,
    actor: ActorContext = Depends(resolve_current_actor),
):
    """턴의 민감 정보(user_message / assistant_response)를 [REDACTED]로 치환.

    감사 로그에 제거 사유, 필드, actor_type 기록.
    """
    ctx = request.state.context
    actor_type_s = _actor_type_str(actor)

    with get_db() as conn:
        repo = ConversationRepository(conn)
        turn_repo = TurnRepository(conn)

        conv = repo.get_by_id(conversation_id)
        if not conv:
            raise not_found("대화를 찾을 수 없습니다.")
        _assert_write_access(conv, actor)

        turn = turn_repo.get_by_id(turn_id)
        if not turn or turn.conversation_id != conversation_id:
            raise not_found("턴을 찾을 수 없습니다.")

        ok = turn_repo.redact_turn(turn_id, body.fields)
        if not ok:
            raise bad_request("제거할 필드가 없습니다.")
        conn.commit()

    audit_emitter.emit(
        event_type="turn.redacted",
        action="turn.redact",
        actor_id=str(actor.actor_id),
        actor_type=actor_type_s,
        resource_type="turn",
        resource_id=turn_id,
        result="success",
        request_id=ctx.request_id,
        metadata={"fields": body.fields, "reason": body.reason},
    )

    return success_response(
        data={"redacted": True, "fields": body.fields},
        request_id=ctx.request_id,
    )
