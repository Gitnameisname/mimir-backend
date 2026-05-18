"""S3 Phase 6 FG 6-4: admin_org_guard 단위 회귀.

회귀 시나리오:
  G1. SUPER_ADMIN 은 임의의 organization 접근 가능 — audit emit 호출.
  G2. ORG_ADMIN 은 본인 조직만 통과.
  G3. ORG_ADMIN 이 다른 조직 자원에 접근 시 ApiPermissionDeniedError.
  G4. target_org_id is None + non-SUPER_ADMIN → 거부.
  G5. 미인증 actor → 거부.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.api.auth.models import ActorContext, ActorType, AuthMethod
from app.api.errors.exceptions import ApiPermissionDeniedError
from app.utils.admin_org_guard import (
    actor_org_ids,
    ensure_actor_can_access_org,
    is_super_admin,
)


def _conn_returning_org_rows(org_ids: list[str]) -> MagicMock:
    """user_org_roles 조회 시 ``[{"org_id": "..."}]`` 를 반환하는 mock conn."""
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchall.return_value = [{"org_id": oid} for oid in org_ids]
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


def _make_actor(role: str, actor_id: str = "u1", authenticated: bool = True) -> ActorContext:
    return ActorContext(
        actor_type=ActorType.USER,
        actor_id=actor_id,
        is_authenticated=authenticated,
        auth_method=AuthMethod.SESSION,
        tenant_id=None,
        role=role,
    )


def test_is_super_admin_only_for_super_admin():
    assert is_super_admin(_make_actor("SUPER_ADMIN")) is True
    assert is_super_admin(_make_actor("ORG_ADMIN")) is False
    assert is_super_admin(_make_actor("VIEWER")) is False
    assert is_super_admin(None) is False


def test_super_admin_can_access_any_org(monkeypatch):
    actor = _make_actor("SUPER_ADMIN")
    conn = _conn_returning_org_rows([])
    emitted: list[dict] = []

    def _fake_emit(**kwargs):
        emitted.append(kwargs)

    from app.utils import admin_org_guard
    monkeypatch.setattr(admin_org_guard.audit_emitter, "emit", _fake_emit)

    ensure_actor_can_access_org(
        conn, actor,
        target_org_id="org-X",
        action="agent.create",
        resource_type="agent",
    )
    assert any(e.get("event_type") == "admin.cross_org_access" for e in emitted)


def test_org_admin_same_org_passes():
    actor = _make_actor("ORG_ADMIN")
    conn = _conn_returning_org_rows(["org-A"])
    # 같은 org → no exception.
    ensure_actor_can_access_org(
        conn, actor,
        target_org_id="org-A",
        action="agent.update",
        resource_type="agent",
    )


def test_org_admin_other_org_rejected():
    actor = _make_actor("ORG_ADMIN")
    conn = _conn_returning_org_rows(["org-A"])
    with pytest.raises(ApiPermissionDeniedError):
        ensure_actor_can_access_org(
            conn, actor,
            target_org_id="org-B",
            action="agent.update",
            resource_type="agent",
        )


def test_target_org_none_rejected_for_non_super_admin():
    actor = _make_actor("ORG_ADMIN")
    conn = _conn_returning_org_rows(["org-A"])
    with pytest.raises(ApiPermissionDeniedError):
        ensure_actor_can_access_org(
            conn, actor,
            target_org_id=None,
            action="agent.update",
            resource_type="agent",
        )


def test_anonymous_rejected():
    actor = _make_actor("ORG_ADMIN", authenticated=False)
    conn = _conn_returning_org_rows([])
    with pytest.raises(ApiPermissionDeniedError):
        ensure_actor_can_access_org(
            conn, actor,
            target_org_id="org-A",
            action="agent.update",
            resource_type="agent",
        )


def test_actor_org_ids_filters_by_role():
    actor = _make_actor("ORG_ADMIN")
    conn = _conn_returning_org_rows(["org-A", "org-B"])
    ids = actor_org_ids(conn, actor, role_filter=frozenset({"ORG_ADMIN"}))
    assert ids == frozenset({"org-A", "org-B"})


def test_actor_org_ids_anonymous_empty():
    actor = _make_actor("VIEWER", actor_id=None, authenticated=False)
    conn = _conn_returning_org_rows(["org-A"])
    assert actor_org_ids(conn, actor) == frozenset()
