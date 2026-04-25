"""
S3 Phase 2 FG 2-2 회귀 — tag RBAC 매트릭스.

tag.list 는 모든 인증 사용자 (VIEWER 이상) 허용.
tag.delete 는 ORG_ADMIN / SUPER_ADMIN 전용.
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


class TestTagActionsRegistered:
    def test_matrix_has_tag_actions(self):
        from app.api.auth.authorization import _PERMISSION_MATRIX
        assert "tag.list" in _PERMISSION_MATRIX
        assert "tag.delete" in _PERMISSION_MATRIX

    def test_tag_list_allows_viewer_and_above(self):
        from app.api.auth.authorization import _PERMISSION_MATRIX
        roles = _PERMISSION_MATRIX["tag.list"]
        for r in ("VIEWER", "AUTHOR", "REVIEWER", "APPROVER", "ORG_ADMIN", "SUPER_ADMIN"):
            assert r in roles, r

    def test_tag_delete_admin_only(self):
        from app.api.auth.authorization import _PERMISSION_MATRIX
        roles = _PERMISSION_MATRIX["tag.delete"]
        assert roles == frozenset({"ORG_ADMIN", "SUPER_ADMIN"})


class TestAuthorizeBehavior:
    def test_viewer_can_list(self):
        from app.api.auth.authorization import ResourceRef, authorization_service
        authorization_service.authorize(
            actor=_actor(role="VIEWER"),
            action="tag.list",
            resource=ResourceRef(resource_type="tag"),
            require_authenticated=True,
        )

    def test_viewer_cannot_delete(self):
        from app.api.auth.authorization import ResourceRef, authorization_service
        from app.api.errors.exceptions import ApiPermissionDeniedError
        with pytest.raises(ApiPermissionDeniedError):
            authorization_service.authorize(
                actor=_actor(role="VIEWER"),
                action="tag.delete",
                resource=ResourceRef(resource_type="tag", resource_id="t1"),
                require_authenticated=True,
            )

    def test_org_admin_can_delete(self):
        from app.api.auth.authorization import ResourceRef, authorization_service
        authorization_service.authorize(
            actor=_actor(role="ORG_ADMIN"),
            action="tag.delete",
            resource=ResourceRef(resource_type="tag", resource_id="t1"),
            require_authenticated=True,
        )

    def test_anonymous_cannot_list(self):
        from app.api.auth.authorization import ResourceRef, authorization_service
        from app.api.errors.exceptions import ApiAuthenticationError
        with pytest.raises(ApiAuthenticationError):
            authorization_service.authorize(
                actor=_actor(role=None, actor_type="anonymous", is_authenticated=False),
                action="tag.list",
                resource=ResourceRef(resource_type="tag"),
                require_authenticated=True,
            )
