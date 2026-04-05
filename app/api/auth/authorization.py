"""
AuthorizationService — authorization hook point.

"이 actor가 이 action/resource를 할 수 있는가"를 판단하는 계층.
현재는 stub이며, 향후 ACL/RBAC/ABAC/policy engine으로 확장된다.

설계 원칙:
  - router는 action/resource를 선언하고 AuthorizationService.authorize()를 호출한다.
  - 실제 허용/거부 판단은 이 계층에서만 일어난다.
  - router에 권한 if문을 직접 쓰지 않는다.

action naming: <resource_type>.<verb>
  예: document.read, document.create, document.update
      version.read, version.create
      node.read

resource reference (ResourceRef):
  - resource_type: 리소스 종류 문자열
  - resource_id:   선택적 식별자 (있는 경우)
  - parent_id:     부모 리소스 ID (예: document_id for version)
  - tenant_id:     멀티테넌시 scope (tenant enforcement 연결 위치)

401 / 403 분리:
  - 인증되지 않은 actor가 protected resource 접근 → ApiAuthenticationError (401)
  - 인증됐지만 권한 없음 → ApiPermissionDeniedError (403)

TODO:
  - ACL/RBAC/ABAC policy engine 연결 예정
  - tenant scope enforcement 구현 예정
  - admin/service caller 세분화 예정
  - tenant membership resolution 예정
  - permission matrix 정의 예정
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
                       예: "document", "version", "node"
        resource_id:   특정 리소스를 가리킬 때 사용. list 조회 시 None.
        parent_id:     계층 리소스의 부모 ID. 예: version의 document_id.
        tenant_id:     tenant scope. 향후 멀티테넌시 enforcement 연결 위치.
    """

    resource_type: str
    resource_id: Optional[str] = None
    parent_id: Optional[str] = None
    tenant_id: Optional[str] = None  # TODO: tenant scope enforcement 연결 예정


# ---------------------------------------------------------------------------
# AuthorizationService
# ---------------------------------------------------------------------------


class AuthorizationService:
    """Authorization hook point.

    라우터는 action/resource를 선언하고 이 서비스를 호출한다.
    현재 stub 구현: 인증 강제 옵션만 지원하며 실제 permission은 미구현.

    향후 확장:
      - ACL/RBAC/ABAC policy engine 연결
      - tenant scope enforcement
      - admin role 판단
      - audit log 연결
    """

    def authorize(
        self,
        actor: ActorContext,
        action: str,
        resource: ResourceRef,
        *,
        require_authenticated: bool = False,
    ) -> None:
        """actor가 action/resource를 수행할 수 있는지 확인한다.

        Args:
            actor:                  resolve_current_actor에서 받은 ActorContext.
            action:                 수행할 동작. "<resource_type>.<verb>" 형식.
                                    예: "document.read", "document.create"
            resource:               대상 리소스 참조.
            require_authenticated:  True이면 anonymous actor를 거부한다.
                                    현재 stub 기간에는 기본 False.
                                    실제 enforcement 활성화 시 True로 전환 예정.

        Raises:
            ApiAuthenticationError:   인증 필요하지만 anonymous 또는 미검증.
            ApiPermissionDeniedError: 인증됐지만 action/resource 권한 없음.
        """
        # 인증 강제 검사
        if require_authenticated:
            if actor.actor_type == ActorType.ANONYMOUS:
                raise ApiAuthenticationError()
            if not actor.is_authenticated:
                # 인증 입력은 있었지만 검증 미완료 (stub 기간)
                raise ApiAuthenticationError(
                    "Authentication could not be verified"
                )

        # TODO: ACL/RBAC/ABAC policy engine 연결 예정
        # TODO: tenant scope enforcement 연결 예정
        #   resource.tenant_id 와 actor.tenant_id 일치 여부 검사 예정
        # TODO: admin/service 세분화 정책 추가 예정
        # 현재 stub: 인증 조건 통과 후 모든 action 허용


# 모듈 레벨 싱글톤 — 라우터에서 import해서 사용
authorization_service = AuthorizationService()
