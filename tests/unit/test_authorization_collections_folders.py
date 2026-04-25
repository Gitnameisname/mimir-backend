"""
S3 Phase 2 FG 2-1 버그픽스 회귀 — collection/folder/document.folder.set 액션이
RBAC 매트릭스에 등록되어 authorize() 가 403 "Unknown action" 을 던지지 않는지 검증.

근거:
  라우터가 `authorization_service.authorize(action="collection.create", ...)` 를 호출하는데
  _PERMISSION_MATRIX 에 키가 없으면 기본적으로 403 "Unknown action" 으로 거부된다.
  2026-04-24 사용자 보고: "컬렉션과 폴더를 만들려고 하는데 수행할 권한이 없다네"
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal")


pytestmark = pytest.mark.unit


# S3 Phase 2 FG 2-1 에서 라우터가 호출하는 action 전집합
_FG21_ACTIONS = [
    # collections
    "collection.list",
    "collection.read",
    "collection.create",
    "collection.update",
    "collection.delete",
    "collection.add_documents",
    "collection.remove_document",
    # folders
    "folder.list",
    "folder.read",
    "folder.create",
    "folder.update",
    "folder.move",
    "folder.delete",
    # document ↔ folder 배치
    "document.folder.set",
]


def _actor(role: str | None, actor_type: str = "user", is_authenticated: bool = True):
    from app.api.auth.models import ActorContext, ActorType
    return ActorContext(
        actor_type=ActorType(actor_type),
        actor_id="u-1" if is_authenticated else None,
        is_authenticated=is_authenticated,
        auth_method=None,
        tenant_id=None,
        role=role,
    )


class TestMatrixCompleteness:
    def test_all_fg21_actions_registered(self):
        """FG 2-1 의 모든 라우터 action 이 _PERMISSION_MATRIX 에 존재해야 한다."""
        from app.api.auth.authorization import _PERMISSION_MATRIX
        missing = [a for a in _FG21_ACTIONS if a not in _PERMISSION_MATRIX]
        assert not missing, (
            f"다음 action 이 _PERMISSION_MATRIX 에 없어 'Unknown action → denied by default' 로 "
            f"403 이 발생합니다: {missing}"
        )

    def test_viewer_allowed_on_all_fg21_actions(self):
        """뷰 레이어이므로 VIEWER 도 포함되어야 한다."""
        from app.api.auth.authorization import _PERMISSION_MATRIX
        for action in _FG21_ACTIONS:
            roles = _PERMISSION_MATRIX[action]
            assert "VIEWER" in roles, (
                f"action={action} 의 허용 role 에 VIEWER 가 없습니다. "
                f"컬렉션/폴더는 뷰 레이어 + owner 격리 이므로 VIEWER 허용이 설계."
            )


class TestAuthorizeBehavior:
    """실제 authorize() 가 권한을 허용/거부하는 동작 검증."""

    @pytest.mark.parametrize("action", _FG21_ACTIONS)
    def test_viewer_passes(self, action):
        from app.api.auth.authorization import authorization_service, ResourceRef
        actor = _actor(role="VIEWER")
        # 예외가 발생하지 않으면 통과
        authorization_service.authorize(
            actor=actor,
            action=action,
            resource=ResourceRef(resource_type=action.split(".")[0]),
            require_authenticated=True,
        )

    @pytest.mark.parametrize("action", ["collection.create", "folder.create", "document.folder.set"])
    def test_anonymous_rejected_with_401(self, action):
        from app.api.auth.authorization import authorization_service, ResourceRef
        from app.api.errors.exceptions import ApiAuthenticationError
        anon = _actor(role=None, actor_type="anonymous", is_authenticated=False)
        with pytest.raises(ApiAuthenticationError):
            authorization_service.authorize(
                actor=anon,
                action=action,
                resource=ResourceRef(resource_type=action.split(".")[0]),
                require_authenticated=True,
            )

    def test_unknown_action_still_rejected(self):
        """매트릭스에 없는 action 은 여전히 403 으로 거부되어야 함 (보안 원칙 유지)."""
        from app.api.auth.authorization import authorization_service, ResourceRef
        from app.api.errors.exceptions import ApiPermissionDeniedError
        actor = _actor(role="SUPER_ADMIN")
        with pytest.raises(ApiPermissionDeniedError, match="Unknown action"):
            authorization_service.authorize(
                actor=actor,
                action="collection.unknown_verb_does_not_exist",
                resource=ResourceRef(resource_type="collection"),
                require_authenticated=True,
            )

    def test_is_allowed_api(self):
        """UI 조건부 렌더용 is_allowed() 가 FG 2-1 액션에 대해 True 를 반환."""
        from app.api.auth.authorization import authorization_service
        actor = _actor(role="VIEWER")
        for action in _FG21_ACTIONS:
            assert authorization_service.is_allowed(actor, action) is True, action


class TestSuperAdminAdminRead:
    """admin.write 는 SUPER_ADMIN 전용 유지 확인 (회귀 방어)."""

    def test_admin_write_still_super_admin_only(self):
        from app.api.auth.authorization import _PERMISSION_MATRIX
        assert _PERMISSION_MATRIX["admin.write"] == frozenset({"SUPER_ADMIN"})

    def test_admin_read_still_admins_only(self):
        from app.api.auth.authorization import _PERMISSION_MATRIX
        assert _PERMISSION_MATRIX["admin.read"] == frozenset({"ORG_ADMIN", "SUPER_ADMIN"})
