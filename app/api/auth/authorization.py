"""
AuthorizationService — RBAC 기반 권한 검사.

"이 actor가 이 action/resource를 할 수 있는가"를 판단하는 계층.

설계 원칙:
  - router는 action/resource를 선언하고 AuthorizationService.authorize()를 호출한다.
  - 실제 허용/거부 판단은 이 계층에서만 일어난다.
  - router에 권한 if문을 직접 쓰지 않는다.

action naming: <resource_type>.<verb>
  예: document.read, document.create, document.update, document.delete
      version.read, version.create
      node.read
      workflow.submit_review, workflow.approve, workflow.reject
      workflow.publish, workflow.archive, workflow.return_to_draft
      admin.read, admin.write

역할 계층 (높을수록 더 많은 권한):
  VIEWER < AUTHOR < REVIEWER < APPROVER < ORG_ADMIN < SUPER_ADMIN

401 / 403 분리:
  - 인증되지 않은 actor가 protected resource 접근 → ApiAuthenticationError (401)
  - 인증됐지만 권한 없음 → ApiPermissionDeniedError (403)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.api.auth.models import ActorContext, ActorType
from app.api.errors.exceptions import ApiAuthenticationError, ApiPermissionDeniedError


# ---------------------------------------------------------------------------
# Resource reference
# ---------------------------------------------------------------------------


@dataclass
class ResourceRef:
    """authorization 판단에 필요한 최소 리소스 맥락.

    Attributes:
        resource_type: 리소스 종류. action의 앞부분과 일치시킨다.
                       예: "document", "version", "node", "workflow", "admin"
        resource_id:   특정 리소스를 가리킬 때 사용. list 조회 시 None.
        parent_id:     계층 리소스의 부모 ID. 예: version의 document_id.
        tenant_id:     tenant scope. 향후 멀티테넌시 enforcement 연결 위치.
    """

    resource_type: str
    resource_id: Optional[str] = None
    parent_id: Optional[str] = None
    tenant_id: Optional[str] = None


# ---------------------------------------------------------------------------
# RBAC 권한 매트릭스
# ---------------------------------------------------------------------------

# action → 허용된 최소 역할 집합
# 나열된 역할 중 하나 이상이면 허용.
# SERVICE actor는 모든 action 허용 (내부 서비스 간 호출).
_PERMISSION_MATRIX: dict[str, frozenset[str]] = {
    # --- 문서 ---
    "document.list":   frozenset({"VIEWER", "AUTHOR", "REVIEWER", "APPROVER", "ORG_ADMIN", "SUPER_ADMIN"}),
    "document.read":   frozenset({"VIEWER", "AUTHOR", "REVIEWER", "APPROVER", "ORG_ADMIN", "SUPER_ADMIN"}),
    "document.render": frozenset({"VIEWER", "AUTHOR", "REVIEWER", "APPROVER", "ORG_ADMIN", "SUPER_ADMIN"}),
    "document.create": frozenset({"AUTHOR", "ORG_ADMIN", "SUPER_ADMIN"}),
    "document.update": frozenset({"AUTHOR", "ORG_ADMIN", "SUPER_ADMIN"}),
    "document.delete": frozenset({"ORG_ADMIN", "SUPER_ADMIN"}),
    "document.publish": frozenset({"APPROVER", "ORG_ADMIN", "SUPER_ADMIN"}),

    # --- 버전 ---
    "version.list":    frozenset({"VIEWER", "AUTHOR", "REVIEWER", "APPROVER", "ORG_ADMIN", "SUPER_ADMIN"}),
    "version.read":    frozenset({"VIEWER", "AUTHOR", "REVIEWER", "APPROVER", "ORG_ADMIN", "SUPER_ADMIN"}),
    "version.render":  frozenset({"VIEWER", "AUTHOR", "REVIEWER", "APPROVER", "ORG_ADMIN", "SUPER_ADMIN"}),
    "version.create":  frozenset({"AUTHOR", "ORG_ADMIN", "SUPER_ADMIN"}),
    "version.restore": frozenset({"AUTHOR", "ORG_ADMIN", "SUPER_ADMIN"}),

    # --- 노드 ---
    "node.list":  frozenset({"VIEWER", "AUTHOR", "REVIEWER", "APPROVER", "ORG_ADMIN", "SUPER_ADMIN"}),
    "node.read":  frozenset({"VIEWER", "AUTHOR", "REVIEWER", "APPROVER", "ORG_ADMIN", "SUPER_ADMIN"}),
    "node.write": frozenset({"AUTHOR", "ORG_ADMIN", "SUPER_ADMIN"}),

    # --- Draft ---
    "draft.save":    frozenset({"AUTHOR", "ORG_ADMIN", "SUPER_ADMIN"}),
    "draft.discard": frozenset({"AUTHOR", "ORG_ADMIN", "SUPER_ADMIN"}),

    # --- 워크플로 ---
    "workflow.submit_review":        frozenset({"AUTHOR", "ORG_ADMIN", "SUPER_ADMIN"}),
    "workflow.approve":               frozenset({"APPROVER", "ORG_ADMIN", "SUPER_ADMIN"}),
    "workflow.reject":                frozenset({"REVIEWER", "APPROVER", "ORG_ADMIN", "SUPER_ADMIN"}),
    "workflow.publish":               frozenset({"APPROVER", "ORG_ADMIN", "SUPER_ADMIN"}),
    "workflow.archive":               frozenset({"ORG_ADMIN", "SUPER_ADMIN"}),
    "workflow.return_to_draft":       frozenset({"AUTHOR", "REVIEWER", "APPROVER", "ORG_ADMIN", "SUPER_ADMIN"}),
    "workflow.history.read":          frozenset({"VIEWER", "AUTHOR", "REVIEWER", "APPROVER", "ORG_ADMIN", "SUPER_ADMIN"}),
    "workflow.review_actions.read":   frozenset({"VIEWER", "AUTHOR", "REVIEWER", "APPROVER", "ORG_ADMIN", "SUPER_ADMIN"}),

    # --- 관리자 ---
    "admin.read":  frozenset({"ORG_ADMIN", "SUPER_ADMIN"}),
    "admin.write": frozenset({"SUPER_ADMIN"}),
}


# ---------------------------------------------------------------------------
# AuthorizationService
# ---------------------------------------------------------------------------


class AuthorizationService:
    """RBAC 기반 Authorization 서비스.

    라우터는 action/resource를 선언하고 이 서비스를 호출한다.
    actor.role이 해당 action의 허용 역할 집합에 포함되면 통과.

    향후 확장:
      - 테넌트 scope enforcement (resource.tenant_id ↔ actor.tenant_id)
      - 문서 소유자 기반 예외 (AUTHOR가 자신의 문서만 수정 가능)
      - ABAC 정책 엔진 연결
    """

    def authorize(
        self,
        actor: ActorContext,
        action: str,
        resource: ResourceRef,
        *,
        require_authenticated: bool = True,
    ) -> None:
        """actor가 action/resource를 수행할 수 있는지 확인한다.

        Args:
            actor:                  resolve_current_actor에서 받은 ActorContext.
            action:                 수행할 동작. "<resource_type>.<verb>" 형식.
            resource:               대상 리소스 참조.
            require_authenticated:  False이면 anonymous도 통과 (read-only 공개 리소스용).

        Raises:
            ApiAuthenticationError:   인증 필요하지만 anonymous 또는 미검증.
            ApiPermissionDeniedError: 인증됐지만 action/resource 권한 없음.
        """
        # --- 1. SERVICE actor는 내부 호출 — 모든 action 허용 ---
        if actor.actor_type == ActorType.SERVICE and actor.is_authenticated:
            return

        # --- 2. 인증 여부 검사 ---
        if require_authenticated:
            if actor.actor_type == ActorType.ANONYMOUS:
                raise ApiAuthenticationError()
            if not actor.is_authenticated:
                raise ApiAuthenticationError("Authentication could not be verified")

        # --- 3. Anonymous + 공개 접근 허용 → RBAC 생략 ---
        # require_authenticated=False이면 anonymous actor는 무조건 통과.
        # 인증된 actor(역할 보유)는 반드시 RBAC 검사를 거친다.
        if not require_authenticated and actor.actor_type == ActorType.ANONYMOUS:
            return

        # --- 4. RBAC 권한 검사 (인증된 actor는 require_authenticated 여부와 무관하게 항상 적용) ---
        allowed_roles = _PERMISSION_MATRIX.get(action)

        if allowed_roles is None:
            # 알 수 없는 action — 보안상 거부
            raise ApiPermissionDeniedError(
                f"Unknown action '{action}'. Access denied by default."
            )

        actor_role = actor.role
        if not actor_role or actor_role not in allowed_roles:
            raise ApiPermissionDeniedError(
                f"Role '{actor_role}' is not authorized for action '{action}'."
            )

    def is_allowed(
        self,
        actor: ActorContext,
        action: str,
    ) -> bool:
        """권한 검사 결과를 bool로 반환한다 (예외 없음).

        UI 조건부 렌더링 등 비중단 확인용.
        """
        try:
            self.authorize(actor, action, ResourceRef(resource_type=""))
            return True
        except (ApiAuthenticationError, ApiPermissionDeniedError):
            return False


# 모듈 레벨 싱글톤 — 라우터에서 import해서 사용
authorization_service = AuthorizationService()
