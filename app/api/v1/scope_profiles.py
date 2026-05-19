"""
Scope Profile CRUD API + Agent 관리 API + Kill Switch API — Phase 4 (S2).
FG5.3: 에이전트 감사 조회 API + 통계 API + Rate Limit 조회 API 추가.

S2 원칙 ⑤: 접근 범위(scope)는 관리자 설정으로 동적 관리.
모든 엔드포인트는 admin 역할 필수.

라우터 경로:
  /admin/scope-profiles              — ScopeProfile CRUD
  /admin/agents                      — Agent CRUD
  /admin/agents/{id}/kill-switch     — Kill Switch
  /admin/agents/{id}/audit           — 에이전트 감사 이력 조회
  /admin/agents/{id}/statistics      — 에이전트 통계
  /admin/agents/{id}/rate-limit      — 에이전트 Rate Limit 현황
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

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
from app.utils.http_errors import not_found, unprocessable_entity
from app.utils.converters import uuid_str_or_none
from app.repositories.pagination import paginate_page

logger = logging.getLogger(__name__)

router = APIRouter()

# 도서관 §1.10 BE-G6 (2026-04-25): ADMIN_ROLES + require_admin 위임.
# 기존 _ADMIN_ROLES 모듈 상수와 _require_admin 함수는 thin re-export 로 호환 유지.
from app.utils.actor import ADMIN_ROLES as _ADMIN_ROLES, require_admin as _require_admin  # noqa: F401

# S3 Phase 6 FG 6-4 (2026-05-18): admin organization 격리 — SUPER_ADMIN 만 횡단 허용.
from app.utils.admin_org_guard import ensure_actor_can_access_org


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
    # S3 Phase 3 FG 3-2 (2026-04-27): settings (ScopeProfileSettings dataclass) → schema
    # S3 Phase 4 FG 4-0 §2.1.6 (2026-04-28): allowed_tools 노출
    from app.schemas.agent import ScopeProfileSettingsSchema  # 지연 import 회피

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
        settings=ScopeProfileSettingsSchema(
            expose_viewers=bool(profile.settings.expose_viewers),
        ),
        allowed_tools=list(profile.allowed_tools or []),
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
    # S3 Phase 3 FG 3-2 (2026-04-27): create 시 settings 도 함께 전달 (옵셔널).
    # S3 Phase 4 FG 4-0 §2.1.6 (2026-04-28): allowed_tools 도 함께 전달 (옵셔널).
    from app.models.scope_profile import ScopeProfileSettings  # 지연 import
    settings_arg = (
        ScopeProfileSettings(expose_viewers=bool(body.settings.expose_viewers))
        if body.settings is not None else None
    )
    try:
        with get_db() as conn:
            # FG 6-4 R-O4: 다른 조직 자원 생성 차단.
            ensure_actor_can_access_org(
                conn, actor,
                target_org_id=body.organization_id,
                action="scope_profile.create",
                resource_type="scope_profile",
            )
            repo = ScopeProfileRepository(conn)
            profile = repo.create(
                name=body.name,
                description=body.description,
                organization_id=body.organization_id,
                settings=settings_arg,
                allowed_tools=body.allowed_tools,
            )
    except ValueError as exc:
        raise unprocessable_entity(str(exc))
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
    # FG 6-4 R-O4: non-SUPER_ADMIN 은 자기 조직만. organization_id 미지정 시 본인 조직
    # 으로 강제, 다른 조직 지정 시 거부 (또는 SUPER_ADMIN 이면 통과).
    from app.utils.admin_org_guard import is_super_admin, actor_org_ids
    with get_db() as conn:
        if not is_super_admin(actor):
            allowed_orgs = actor_org_ids(
                conn, actor, role_filter=frozenset({"ORG_ADMIN", "SUPER_ADMIN"}),
            )
            if organization_id is None:
                # 미지정 — 본인 첫 org 으로 강제. (다중 조직 admin 은 향후 확장)
                if not allowed_orgs:
                    return ScopeProfileListResponse(
                        items=[], total=0, limit=limit, offset=offset,
                    )
                organization_id = sorted(allowed_orgs)[0]
            elif str(organization_id) not in allowed_orgs:
                ensure_actor_can_access_org(
                    conn, actor,
                    target_org_id=organization_id,
                    action="scope_profile.list",
                    resource_type="scope_profile",
                )
        else:
            # SUPER_ADMIN 이면서 organization_id 명시 시 횡단 audit 자동 emit.
            if organization_id is not None:
                ensure_actor_can_access_org(
                    conn, actor,
                    target_org_id=organization_id,
                    action="scope_profile.list",
                    resource_type="scope_profile",
                )
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
            raise not_found("Scope Profile을 찾을 수 없습니다.")
        # FG 6-4 R-O4: 본 actor 조직 자원만 노출. SUPER_ADMIN 만 횡단 (audit emit).
        ensure_actor_can_access_org(
            conn, actor,
            target_org_id=profile.organization_id,
            action="scope_profile.read",
            resource_type="scope_profile",
            resource_id=profile_id,
        )
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

    # S3 Phase 3 FG 3-2 (2026-04-27): settings PATCH 처리.
    settings_patch: Optional[dict] = None
    settings_before: Optional[dict] = None
    if body.settings is not None:
        settings_patch = {"expose_viewers": bool(body.settings.expose_viewers)}

    # S3 Phase 4 FG 4-0 §2.1.6 (2026-04-28): allowed_tools 변경 audit 를 위해 변경 전 값 캡쳐
    allowed_tools_before: Optional[list[str]] = None

    try:
        with get_db() as conn:
            affected_agents = _get_affected_agents(conn, profile_id)
            repo = ScopeProfileRepository(conn)
            # FG 6-4 R-O4: 본 actor 조직 자원만 수정. 변경 전 상태 캡쳐 시 함께 확인.
            existing = repo.get_by_id(profile_id)
            if existing is None:
                raise not_found("Scope Profile을 찾을 수 없습니다.")
            ensure_actor_can_access_org(
                conn, actor,
                target_org_id=existing.organization_id,
                action="scope_profile.update",
                resource_type="scope_profile",
                resource_id=profile_id,
            )
            # settings.changed / allowed_tools.changed audit 를 위해 변경 전 값 캡쳐
            if settings_patch is not None or body.allowed_tools is not None:
                if settings_patch is not None:
                    settings_before = {
                        "expose_viewers": bool(existing.settings.expose_viewers),
                    }
                if body.allowed_tools is not None:
                    allowed_tools_before = list(existing.allowed_tools or [])
            profile = repo.update(
                profile_id,
                name=body.name,
                description=body.description,
                settings_patch=settings_patch,
                allowed_tools=body.allowed_tools,
            )
    except ValueError as exc:
        raise unprocessable_entity(str(exc))
    if not profile:
        raise not_found("Scope Profile을 찾을 수 없습니다.")
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
    # S3 Phase 4 FG 4-0 §2.1.6 (2026-04-28): allowed_tools 변경 별도 audit (보안 정책 변경 추적)
    if body.allowed_tools is not None and allowed_tools_before is not None:
        if sorted(allowed_tools_before) != sorted(profile.allowed_tools or []):
            audit_emitter.emit(
                event_type="scope_profile.allowed_tools.changed",
                action="scope_profile.allowed_tools.update",
                actor_id=actor.resolved_id,
                actor_type=actor.audit_actor_type,
                resource_type="scope_profile",
                resource_id=profile_id,
                result="success",
                metadata={
                    "before": allowed_tools_before,
                    "after": list(profile.allowed_tools or []),
                    "affected_agents": affected_agents,
                },
            )
    # S3 Phase 3 FG 3-2 (2026-04-27): settings 변경 별도 audit + 정책 캐시 invalidate.
    # S3 Phase 7 FG 7-3 (2026-05-18): broadcast=True — cluster-wide 즉시 invalidate.
    if settings_patch is not None:
        from app.services.scope_profile_policy import invalidate_cache
        invalidate_cache(profile_id, broadcast=True)
        audit_emitter.emit(
            event_type="scope_profile.settings.changed",
            action="scope_profile.settings.update",
            actor_id=actor.resolved_id,
            actor_type=actor.audit_actor_type,
            resource_type="scope_profile",
            resource_id=profile_id,
            result="success",
            metadata={
                "before": settings_before or {},
                "after": settings_patch,
            },
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
        # FG 6-4 R-O4: 본 actor 조직 자원만 삭제.
        existing = repo.get_by_id(profile_id)
        if existing is None:
            raise not_found("Scope Profile을 찾을 수 없습니다.")
        ensure_actor_can_access_org(
            conn, actor,
            target_org_id=existing.organization_id,
            action="scope_profile.delete",
            resource_type="scope_profile",
            resource_id=profile_id,
        )
        deleted = repo.delete(profile_id)
    if not deleted:
        raise not_found("Scope Profile을 찾을 수 없습니다.")
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
        raise unprocessable_entity(f"acl_filter 오류: {exc}")

    with get_db() as conn:
        sp_repo = ScopeProfileRepository(conn)
        profile = sp_repo.get_by_id(profile_id)
        if not profile:
            raise not_found("Scope Profile을 찾을 수 없습니다.")
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
        raise not_found("Scope Definition을 찾을 수 없습니다.")


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
        # FG 6-4 R-O4: 다른 조직 agent 생성 차단.
        ensure_actor_can_access_org(
            conn, actor,
            target_org_id=body.organization_id,
            action="agent.create",
            resource_type="agent",
        )
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
    # FG 6-4 R-O4: non-SUPER_ADMIN 은 자기 조직만.
    from app.utils.admin_org_guard import is_super_admin, actor_org_ids
    with get_db() as conn:
        if not is_super_admin(actor):
            allowed_orgs = actor_org_ids(
                conn, actor, role_filter=frozenset({"ORG_ADMIN", "SUPER_ADMIN"}),
            )
            if organization_id is None:
                if not allowed_orgs:
                    return AgentListResponse(items=[], total=0, limit=limit, offset=offset)
                organization_id = sorted(allowed_orgs)[0]
            elif str(organization_id) not in allowed_orgs:
                ensure_actor_can_access_org(
                    conn, actor,
                    target_org_id=organization_id,
                    action="agent.list",
                    resource_type="agent",
                )
        elif organization_id is not None:
            ensure_actor_can_access_org(
                conn, actor,
                target_org_id=organization_id,
                action="agent.list",
                resource_type="agent",
            )
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
            raise not_found("에이전트를 찾을 수 없습니다.")
        # FG 6-4 R-O4.
        ensure_actor_can_access_org(
            conn, actor,
            target_org_id=agent.organization_id,
            action="agent.read",
            resource_type="agent",
            resource_id=agent_id,
        )
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
        existing = repo.get_by_id(agent_id)
        if not existing:
            raise not_found("에이전트를 찾을 수 없습니다.")
        # FG 6-4 R-O4.
        ensure_actor_can_access_org(
            conn, actor,
            target_org_id=existing.organization_id,
            action="agent.update",
            resource_type="agent",
            resource_id=agent_id,
        )
        agent = repo.update(
            agent_id,
            name=body.name,
            description=body.description,
            scope_profile_id=body.scope_profile_id,
        )
    if not agent:
        raise not_found("에이전트를 찾을 수 없습니다.")
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
        existing = repo.get_by_id(agent_id)
        if not existing:
            raise not_found("에이전트를 찾을 수 없습니다.")
        # FG 6-4 R-O4.
        ensure_actor_can_access_org(
            conn, actor,
            target_org_id=existing.organization_id,
            action="agent.delete",
            resource_type="agent",
            resource_id=agent_id,
        )
        deleted = repo.delete(agent_id)
    if not deleted:
        raise not_found("에이전트를 찾을 수 없습니다.")
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
async def activate_kill_switch(
    agent_id: str,
    body: KillSwitchActivate,
    actor: ActorContext = Depends(resolve_current_actor),
):
    _require_admin(actor)
    with get_db() as conn:
        repo = AgentRepository(conn)
        agent = repo.enable_kill_switch(agent_id, reason=body.reason)
    if not agent:
        raise not_found("에이전트를 찾을 수 없습니다.")
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
        raise not_found("에이전트를 찾을 수 없습니다.")
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


# ===========================================================================
# FG5.3: 에이전트 감사 조회 / 통계 / Rate Limit 현황
# ===========================================================================

class AgentAuditItem(BaseModel):
    id: str
    event_type: str
    occurred_at: str
    actor_id: Optional[str]
    actor_type: Optional[str]
    acting_on_behalf_of: Optional[str]
    resource_type: Optional[str]
    resource_id: Optional[str]
    previous_state: Optional[str]
    new_state: Optional[str]
    action_result: str
    reason: Optional[str]


class AgentAuditListResponse(BaseModel):
    items: List[AgentAuditItem]
    total: int
    page: int
    page_size: int


class RejectionReasonItem(BaseModel):
    reason: Optional[str]
    count: int


class AgentStatisticsResponse(BaseModel):
    agent_id: str
    agent_name: Optional[str]
    total_proposals: int
    approved_count: int
    rejected_count: int
    withdrawn_count: int
    approval_rate: float
    average_review_time_minutes: Optional[float]
    last_activity: Optional[str]
    rejection_reasons: List[RejectionReasonItem]


class AgentRateLimitResponse(BaseModel):
    agent_id: str
    endpoints: List[dict]


@router.get(
    "/agents/{agent_id}/audit",
    response_model=AgentAuditListResponse,
    summary="에이전트 감사 이력 조회 (FG5.3)",
)
def get_agent_audit(
    agent_id: str,
    start_date: Optional[str] = Query(None, description="시작 날짜 (ISO8601)"),
    end_date: Optional[str] = Query(None, description="종료 날짜 (ISO8601)"),
    action_type: Optional[str] = Query(None, description="이벤트 타입 필터"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    actor: ActorContext = Depends(resolve_current_actor),
):
    """에이전트의 감사 이벤트 이력을 조회한다."""
    _require_admin(actor)

    conditions = [
        "actor_user_id = %s",
        "actor_type = 'agent'",
    ]
    params: list[Any] = [agent_id]

    if action_type:
        conditions.append("event_type = %s")
        params.append(action_type)
    if start_date:
        conditions.append("occurred_at >= %s")
        params.append(start_date)
    if end_date:
        conditions.append("occurred_at <= %s")
        params.append(end_date)

    where = " AND ".join(conditions)
    page, page_size, offset = paginate_page(page, page_size)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM audit_events WHERE {where}", params)
            total = cur.fetchone()["count"]

            cur.execute(
                f"""
                SELECT id, event_type, occurred_at, actor_user_id,
                       actor_type, acting_on_behalf_of,
                       CASE WHEN document_id IS NOT NULL THEN 'document'
                            WHEN version_id IS NOT NULL THEN 'version'
                            ELSE NULL END AS resource_type,
                       COALESCE(CAST(document_id AS TEXT), CAST(version_id AS TEXT)) AS resource_id,
                       previous_state, new_state, action_result, reason
                FROM audit_events
                WHERE {where}
                ORDER BY occurred_at DESC
                LIMIT %s OFFSET %s
                """,
                [*params, page_size, offset],
            )
            rows = cur.fetchall()

    items = [
        AgentAuditItem(
            id=str(r["id"]),
            event_type=r["event_type"],
            occurred_at=r["occurred_at"].isoformat() if hasattr(r["occurred_at"], "isoformat") else str(r["occurred_at"]),
            actor_id=r["actor_user_id"],
            actor_type=r["actor_type"],
            acting_on_behalf_of=r.get("acting_on_behalf_of"),
            resource_type=r.get("resource_type"),
            resource_id=r.get("resource_id"),
            previous_state=r.get("previous_state"),
            new_state=r.get("new_state"),
            action_result=r["action_result"],
            reason=r.get("reason"),
        )
        for r in rows
    ]
    return AgentAuditListResponse(items=items, total=total, page=page, page_size=page_size)


@router.get(
    "/agents/{agent_id}/statistics",
    response_model=AgentStatisticsResponse,
    summary="에이전트 통계 조회 (FG5.3)",
)
def get_agent_statistics(
    agent_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
):
    """에이전트의 제안 통계를 조회한다 (승인율, 평균 검토 시간, 반려 사유 분석)."""
    _require_admin(actor)

    with get_db() as conn:
        # 에이전트 이름 조회
        repo = AgentRepository(conn)
        agent = repo.get(agent_id)
        agent_name = agent.name if agent else None

        with conn.cursor() as cur:
            # 제안 카운트 집계
            cur.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status = 'approved') AS approved_count,
                    COUNT(*) FILTER (WHERE status = 'rejected') AS rejected_count,
                    COUNT(*) FILTER (WHERE status = 'withdrawn') AS withdrawn_count,
                    AVG(
                        EXTRACT(EPOCH FROM (review_timestamp - created_at)) / 60
                    ) FILTER (WHERE review_timestamp IS NOT NULL AND status IN ('approved', 'rejected'))
                        AS avg_review_minutes,
                    MAX(updated_at) AS last_activity
                FROM agent_proposals
                WHERE agent_id = %s
                """,
                (agent_id,),
            )
            row = cur.fetchone()

            total = row["total"] or 0
            approved_count = row["approved_count"] or 0
            rejected_count = row["rejected_count"] or 0
            withdrawn_count = row["withdrawn_count"] or 0
            avg_review_minutes = float(row["avg_review_minutes"]) if row["avg_review_minutes"] is not None else None
            last_activity = row["last_activity"]

            approval_rate = round(approved_count / total, 4) if total > 0 else 0.0

            # 반려 사유 분석
            cur.execute(
                """
                SELECT review_notes AS reason, COUNT(*) AS count
                FROM agent_proposals
                WHERE agent_id = %s AND status = 'rejected'
                GROUP BY review_notes
                ORDER BY count DESC
                LIMIT 10
                """,
                (agent_id,),
            )
            rejection_rows = cur.fetchall()

    rejection_reasons = [
        RejectionReasonItem(reason=r["reason"], count=r["count"])
        for r in rejection_rows
    ]

    return AgentStatisticsResponse(
        agent_id=agent_id,
        agent_name=agent_name,
        total_proposals=total,
        approved_count=approved_count,
        rejected_count=rejected_count,
        withdrawn_count=withdrawn_count,
        approval_rate=approval_rate,
        average_review_time_minutes=avg_review_minutes,
        last_activity=last_activity.isoformat() if last_activity and hasattr(last_activity, "isoformat") else uuid_str_or_none(last_activity),
        rejection_reasons=rejection_reasons,
    )


@router.get(
    "/agents/{agent_id}/rate-limit",
    response_model=AgentRateLimitResponse,
    summary="에이전트 Rate Limit 현황 조회 (FG5.3)",
)
def get_agent_rate_limit(
    agent_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
):
    """에이전트의 현재 Rate Limit 카운터 현황을 조회한다."""
    _require_admin(actor)

    endpoints_info: list[dict] = []

    try:
        from app.cache.valkey import get_valkey
        r = get_valkey()
        # FG5.3: 에이전트별 rate limit 키 패턴: agent:{agent_id}:rate:{endpoint}
        pattern = f"agent:{agent_id}:rate:*"
        keys = r.keys(pattern)
        for key in keys:
            endpoint_name = key.replace(f"agent:{agent_id}:rate:", "")
            current = r.get(key)
            ttl = r.ttl(key)
            endpoints_info.append({
                "endpoint": endpoint_name,
                "current_count": int(current) if current else 0,
                "ttl_seconds": ttl,
            })
    except Exception as exc:
        logger.warning("Rate limit Valkey 조회 실패: %s", exc)
        endpoints_info = []

    return AgentRateLimitResponse(agent_id=agent_id, endpoints=endpoints_info)
