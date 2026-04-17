"""
Scope Profile CRUD API + Agent 관리 API + Kill Switch API — Phase 4 (S2).

S2 원칙 ⑤: 접근 범위(scope)는 관리자 설정으로 동적 관리.
모든 엔드포인트는 admin 역할 필수.

라우터 경로:
  /admin/scope-profiles     — ScopeProfile CRUD
  /admin/agents             — Agent CRUD
  /admin/agents/{id}/kill-switch — Kill Switch
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.auth.dependencies import resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.errors.exceptions import ApiPermissionDeniedError
from app.audit.emitter import audit_emitter
from app.db.connection import get_db
from app.repositories.agent_repository import AgentRepository
from app.repositories.scope_profile_repository import ScopeProfileRepository
from app.schemas.agent import (
    AgentCreate,
    AgentListResponse,
    AgentResponse,
    AgentUpdate,
    KillSwitchActivate,
    KillSwitchResponse,
    ScopeDefinitionCreate,
    ScopeDefinitionSchema,
    ScopeProfileCreate,
    ScopeProfileListResponse,
    ScopeProfileResponse,
    ScopeProfileUpdate,
)
from app.services.filter_expression import parse_filter_expression

logger = logging.getLogger(__name__)

router = APIRouter()

_ADMIN_ROLES = frozenset({"ORG_ADMIN", "SUPER_ADMIN"})


def _require_admin(actor: ActorContext) -> None:
    if not actor.is_authenticated or actor.role not in _ADMIN_ROLES:
        raise ApiPermissionDeniedError("관리자 권한이 필요합니다.")


def _get_affected_agents(conn, profile_id: str) -> list[dict]:
    """Scope Profile에 바인딩된 에이전트 목록을 반환한다."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name FROM agents WHERE scope_profile_id = %s AND is_disabled = FALSE",
            (profile_id,),
        )
        return [{"id": str(r["id"]), "name": r["name"]} for r in cur.fetchall()]


def _agent_response(agent) -> AgentResponse:
    return AgentResponse(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        organization_id=agent.organization_id,
        scope_profile_id=agent.scope_profile_id,
        is_disabled=agent.is_disabled,
        disabled_at=agent.disabled_at,
        disabled_reason=agent.disabled_reason,
        metadata=agent.metadata,
        created_by=agent.created_by,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


def _profile_response(profile) -> ScopeProfileResponse:
    return ScopeProfileResponse(
        id=profile.id,
        name=profile.name,
        description=profile.description,
        organization_id=profile.organization_id,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
        scopes=[
            ScopeDefinitionSchema(
                id=s.id,
                scope_profile_id=s.scope_profile_id,
                scope_name=s.scope_name,
                description=s.description,
                acl_filter=s.acl_filter,
                created_at=s.created_at,
            )
            for s in profile.scopes
        ],
    )


# ===========================================================================
# Scope Profile CRUD
# ===========================================================================

@router.post(
    "/scope-profiles",
    response_model=ScopeProfileResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Scope Profile 생성",
)
def create_scope_profile(
    body: ScopeProfileCreate,
    actor: ActorContext = Depends(resolve_current_actor),
):
    _require_admin(actor)
    with get_db() as conn:
        repo = ScopeProfileRepository(conn)
        profile = repo.create(
            name=body.name,
            description=body.description,
            organization_id=body.organization_id,
        )
    audit_emitter.emit(
        event_type="scope_profile.created",
        action="scope_profile.create",
        actor_id=actor.resolved_id,
        actor_type=actor.audit_actor_type,
        resource_type="scope_profile",
        resource_id=profile.id,
        result="success",
    )
    return _profile_response(profile)


@router.get(
    "/scope-profiles",
    response_model=ScopeProfileListResponse,
    summary="Scope Profile 목록 조회",
)
def list_scope_profiles(
    organization_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    actor: ActorContext = Depends(resolve_current_actor),
):
    _require_admin(actor)
    with get_db() as conn:
        repo = ScopeProfileRepository(conn)
        profiles = repo.list_profiles(organization_id=organization_id, limit=limit, offset=offset)
        total = repo.count(organization_id=organization_id)
    return ScopeProfileListResponse(
        items=[_profile_response(p) for p in profiles],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/scope-profiles/{profile_id}",
    response_model=ScopeProfileResponse,
    summary="Scope Profile 상세 조회",
)
def get_scope_profile(
    profile_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
):
    _require_admin(actor)
    with get_db() as conn:
        repo = ScopeProfileRepository(conn)
        profile = repo.get_by_id(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Scope Profile을 찾을 수 없습니다.")
    return _profile_response(profile)


@router.put(
    "/scope-profiles/{profile_id}",
    response_model=ScopeProfileResponse,
    summary="Scope Profile 수정",
)
def update_scope_profile(
    profile_id: str,
    body: ScopeProfileUpdate,
    actor: ActorContext = Depends(resolve_current_actor),
):
    _require_admin(actor)
    with get_db() as conn:
        affected_agents = _get_affected_agents(conn, profile_id)
        repo = ScopeProfileRepository(conn)
        profile = repo.update(profile_id, name=body.name, description=body.description)
    if not profile:
        raise HTTPException(status_code=404, detail="Scope Profile을 찾을 수 없습니다.")
    audit_emitter.emit(
        event_type="scope_profile.updated",
        action="scope_profile.update",
        actor_id=actor.resolved_id,
        actor_type=actor.audit_actor_type,
        resource_type="scope_profile",
        resource_id=profile_id,
        result="success",
        metadata={"affected_agents": affected_agents},
    )
    return _profile_response(profile)


@router.delete(
    "/scope-profiles/{profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Scope Profile 삭제",
)
def delete_scope_profile(
    profile_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
):
    _require_admin(actor)
    with get_db() as conn:
        affected_agents = _get_affected_agents(conn, profile_id)
        repo = ScopeProfileRepository(conn)
        deleted = repo.delete(profile_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Scope Profile을 찾을 수 없습니다.")
    audit_emitter.emit(
        event_type="scope_profile.deleted",
        action="scope_profile.delete",
        actor_id=actor.resolved_id,
        actor_type=actor.audit_actor_type,
        resource_type="scope_profile",
        resource_id=profile_id,
        result="success",
        metadata={"affected_agents": affected_agents},
    )


@router.post(
    "/scope-profiles/{profile_id}/scopes",
    response_model=ScopeDefinitionSchema,
    status_code=status.HTTP_201_CREATED,
    summary="Scope Definition 추가/갱신",
)
def upsert_scope_definition(
    profile_id: str,
    body: ScopeDefinitionCreate,
    actor: ActorContext = Depends(resolve_current_actor),
):
    _require_admin(actor)
    # FilterExpression 유효성 검증
    try:
        parse_filter_expression(body.acl_filter)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"acl_filter 오류: {exc}")

    with get_db() as conn:
        sp_repo = ScopeProfileRepository(conn)
        profile = sp_repo.get_by_id(profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="Scope Profile을 찾을 수 없습니다.")
        sd = sp_repo.add_definition(
            profile_id,
            scope_name=body.scope_name,
            acl_filter=body.acl_filter,
            description=body.description,
        )
    return ScopeDefinitionSchema(
        id=sd.id,
        scope_profile_id=sd.scope_profile_id,
        scope_name=sd.scope_name,
        description=sd.description,
        acl_filter=sd.acl_filter,
        created_at=sd.created_at,
    )


@router.delete(
    "/scope-profiles/{profile_id}/scopes/{scope_name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Scope Definition 삭제",
)
def delete_scope_definition(
    profile_id: str,
    scope_name: str,
    actor: ActorContext = Depends(resolve_current_actor),
):
    _require_admin(actor)
    with get_db() as conn:
        repo = ScopeProfileRepository(conn)
        deleted = repo.delete_definition(profile_id, scope_name)
    if not deleted:
        raise HTTPException(status_code=404, detail="Scope Definition을 찾을 수 없습니다.")


# ===========================================================================
# Agent CRUD
# ===========================================================================

@router.post(
    "/agents",
    response_model=AgentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="에이전트 생성",
)
def create_agent(
    body: AgentCreate,
    actor: ActorContext = Depends(resolve_current_actor),
):
    _require_admin(actor)
    with get_db() as conn:
        repo = AgentRepository(conn)
        agent = repo.create(
            name=body.name,
            description=body.description,
            organization_id=body.organization_id,
            scope_profile_id=body.scope_profile_id,
            created_by=actor.resolved_id,
            metadata=body.metadata,
        )
    audit_emitter.emit(
        event_type="agent.created",
        action="agent.create",
        actor_id=actor.resolved_id,
        actor_type=actor.audit_actor_type,
        resource_type="agent",
        resource_id=agent.id,
        result="success",
    )
    return _agent_response(agent)


@router.get(
    "/agents",
    response_model=AgentListResponse,
    summary="에이전트 목록 조회",
)
def list_agents(
    organization_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    actor: ActorContext = Depends(resolve_current_actor),
):
    _require_admin(actor)
    with get_db() as conn:
        repo = AgentRepository(conn)
        agents = repo.list_agents(organization_id=organization_id, limit=limit, offset=offset)
        total = repo.count(organization_id=organization_id)
    return AgentListResponse(
        items=[_agent_response(a) for a in agents],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/agents/{agent_id}",
    response_model=AgentResponse,
    summary="에이전트 상세 조회",
)
def get_agent(
    agent_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
):
    _require_admin(actor)
    with get_db() as conn:
        repo = AgentRepository(conn)
        agent = repo.get_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="에이전트를 찾을 수 없습니다.")
    return _agent_response(agent)


@router.put(
    "/agents/{agent_id}",
    response_model=AgentResponse,
    summary="에이전트 수정",
)
def update_agent(
    agent_id: str,
    body: AgentUpdate,
    actor: ActorContext = Depends(resolve_current_actor),
):
    _require_admin(actor)
    with get_db() as conn:
        repo = AgentRepository(conn)
        agent = repo.update(
            agent_id,
            name=body.name,
            description=body.description,
            scope_profile_id=body.scope_profile_id,
        )
    if not agent:
        raise HTTPException(status_code=404, detail="에이전트를 찾을 수 없습니다.")
    audit_emitter.emit(
        event_type="agent.updated",
        action="agent.update",
        actor_id=actor.resolved_id,
        actor_type=actor.audit_actor_type,
        resource_type="agent",
        resource_id=agent_id,
        result="success",
    )
    return _agent_response(agent)


@router.delete(
    "/agents/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="에이전트 삭제",
)
def delete_agent(
    agent_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
):
    _require_admin(actor)
    with get_db() as conn:
        repo = AgentRepository(conn)
        deleted = repo.delete(agent_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="에이전트를 찾을 수 없습니다.")
    audit_emitter.emit(
        event_type="agent.deleted",
        action="agent.delete",
        actor_id=actor.resolved_id,
        actor_type=actor.audit_actor_type,
        resource_type="agent",
        resource_id=agent_id,
        result="success",
    )


# ===========================================================================
# Kill Switch
# ===========================================================================

@router.post(
    "/agents/{agent_id}/kill-switch",
    response_model=KillSwitchResponse,
    summary="에이전트 킬스위치 활성화 — 즉시 쓰기 차단",
)
def activate_kill_switch(
    agent_id: str,
    body: KillSwitchActivate,
    actor: ActorContext = Depends(resolve_current_actor),
):
    _require_admin(actor)
    with get_db() as conn:
        repo = AgentRepository(conn)
        agent = repo.enable_kill_switch(agent_id, reason=body.reason)
    if not agent:
        raise HTTPException(status_code=404, detail="에이전트를 찾을 수 없습니다.")
    audit_emitter.emit(
        event_type="agent.kill_switch_activated",
        action="agent.kill_switch.activate",
        actor_id=actor.resolved_id,
        actor_type=actor.audit_actor_type,
        resource_type="agent",
        resource_id=agent_id,
        result="success",
        metadata={"reason": body.reason},
    )
    return KillSwitchResponse(
        agent_id=agent_id,
        is_disabled=True,
        disabled_at=agent.disabled_at,
        disabled_reason=agent.disabled_reason,
        message="킬스위치가 활성화되었습니다. 해당 에이전트의 쓰기 요청이 즉시 거부됩니다.",
    )


@router.delete(
    "/agents/{agent_id}/kill-switch",
    response_model=KillSwitchResponse,
    summary="에이전트 킬스위치 해제",
)
def deactivate_kill_switch(
    agent_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
):
    _require_admin(actor)
    with get_db() as conn:
        repo = AgentRepository(conn)
        agent = repo.disable_kill_switch(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="에이전트를 찾을 수 없습니다.")
    audit_emitter.emit(
        event_type="agent.kill_switch_deactivated",
        action="agent.kill_switch.deactivate",
        actor_id=actor.resolved_id,
        actor_type=actor.audit_actor_type,
        resource_type="agent",
        resource_id=agent_id,
        result="success",
    )
    return KillSwitchResponse(
        agent_id=agent_id,
        is_disabled=False,
        disabled_at=None,
        disabled_reason=None,
        message="킬스위치가 해제되었습니다. 에이전트 요청이 정상 처리됩니다.",
    )
