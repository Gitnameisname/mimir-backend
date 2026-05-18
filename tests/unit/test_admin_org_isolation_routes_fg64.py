"""S3 Phase 6 FG 6-4 — Admin endpoint 격리 route-level 회귀.

Codex 2차 P2-1 시정 (2026-05-18). `ensure_actor_can_access_org` 의 단위 분기 외에
**실제 ASGI stack 위에서** organization 격리가 작동하는지 검증한다.

회귀 영역:
  A. ORG_ADMIN 이 다른 조직 자원에 접근하면 403 (route-level).
  B. SUPER_ADMIN 이 다른 조직 자원에 접근하면 통과 + `admin.cross_org_access`
     audit event 발생.

본 회귀는 testcontainers / 실 DB 없이 in-process 합성한다 — repository / DB cursor
는 mock 이지만, 라우터 → guard → audit emit 흐름 자체는 실제로 실행된다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "fg64-route-test")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "fg64-route-internal")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_actor(*, role: str, actor_id: str = "00000000-0000-0000-0000-00000000000a"):
    from app.api.auth.models import ActorContext, ActorType, AuthMethod
    return ActorContext(
        actor_type=ActorType.USER,
        actor_id=actor_id,
        is_authenticated=True,
        auth_method=AuthMethod.BEARER,
        tenant_id=None,
        role=role,
    )


def _conn_with_user_org_ids(org_ids: list[str]) -> MagicMock:
    """`user_org_roles` SELECT 가 ``[{"org_id": ...}]`` 를 반환하는 mock conn.

    `actor_org_ids` 가 항상 `cur.fetchall()` 만 사용하므로 다른 SQL 응답은 필요 없음.
    """
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchall.return_value = [{"org_id": oid} for oid in org_ids]

    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


# ---------------------------------------------------------------------------
# Route: DELETE /api/v1/admin/agents/{id} — ORG_ADMIN cross-org reject
# ---------------------------------------------------------------------------


def test_route_agent_delete_cross_org_reject():
    """ORG_ADMIN (org-A) 이 org-B 의 agent 삭제 시도 → 403 + audit emit 0."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.auth import resolve_current_actor
    from app.api.v1 import scope_profiles as scope_profiles_module

    # 1. actor — ORG_ADMIN, org-A 소속.
    actor = _make_actor(role="ORG_ADMIN")
    app.dependency_overrides[resolve_current_actor] = lambda: actor

    # 2. AgentRepository mock — 다른 조직 (org-B) 의 agent 반환.
    target_agent_id = "00000000-0000-0000-0000-0000000000b0"
    fake_agent = MagicMock()
    fake_agent.organization_id = "org-B"

    captured_repos: list[MagicMock] = []

    class FakeAgentRepo:
        def __init__(self, conn):
            self.conn = conn
            captured_repos.append(self)
        def get_by_id(self, _id):
            return fake_agent
        def delete(self, _id):
            return True  # 통과되면 200/204 — 실제로는 호출되어선 안 됨.

    original_repo = scope_profiles_module.AgentRepository
    scope_profiles_module.AgentRepository = FakeAgentRepo

    # 3. get_db 패치 — actor_org_ids 가 SELECT 할 mock conn 반환.
    conn = _conn_with_user_org_ids(["org-A"])
    original_get_db = scope_profiles_module.get_db
    scope_profiles_module.get_db = lambda: conn

    # 4. audit_emitter 패치 — 호출 추적.
    emitted: list[dict] = []
    from app.utils import admin_org_guard
    original_emit = admin_org_guard.audit_emitter.emit
    admin_org_guard.audit_emitter.emit = lambda **kw: emitted.append(kw)

    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete(f"/api/v1/admin/agents/{target_agent_id}")
        # ApiPermissionDeniedError → 403 (mimir error handler 매핑).
        assert resp.status_code == 403, resp.text
        # cross-org 거부 경로 — SUPER_ADMIN 가 아니므로 cross_org_access emit 없음.
        assert not any(e.get("event_type") == "admin.cross_org_access" for e in emitted)
    finally:
        scope_profiles_module.AgentRepository = original_repo
        scope_profiles_module.get_db = original_get_db
        admin_org_guard.audit_emitter.emit = original_emit
        app.dependency_overrides.pop(resolve_current_actor, None)


# ---------------------------------------------------------------------------
# Route: DELETE /api/v1/admin/agents/{id} — SUPER_ADMIN cross-org allows + audit
# ---------------------------------------------------------------------------


def test_route_scope_profile_delete_cross_org_reject():
    """ORG_ADMIN (org-A) 이 org-B 의 scope-profile 삭제 시도 → 403.

    agent 외 다른 endpoint group 의 guard 가 실제로 라우터에 wired 되어 있는지
    검증 (Codex 3차 P2-1 잔존 보강).
    """
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.auth import resolve_current_actor
    from app.api.v1 import scope_profiles as scope_profiles_module

    actor = _make_actor(role="ORG_ADMIN")
    app.dependency_overrides[resolve_current_actor] = lambda: actor

    profile_id = "00000000-0000-0000-0000-0000000000d0"
    fake_profile = MagicMock()
    fake_profile.organization_id = "org-B"

    class FakeScopeProfileRepo:
        def __init__(self, conn):
            self.conn = conn
        def get_by_id(self, _id):
            return fake_profile
        def delete(self, _id):
            return True

    # `_get_affected_agents` 가 conn.cursor SELECT 를 호출 — 빈 리스트 반환.
    original_repo = scope_profiles_module.ScopeProfileRepository
    scope_profiles_module.ScopeProfileRepository = FakeScopeProfileRepo

    # _get_affected_agents 은 internal — fetchall 시퀀스: 1) affected agents [], 2) actor_org_ids [{org-A}]
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchall.side_effect = [
        [],                                  # _get_affected_agents
        [{"org_id": "org-A"}],                # actor_org_ids
    ]
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    original_get_db = scope_profiles_module.get_db
    scope_profiles_module.get_db = lambda: conn

    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete(f"/api/v1/admin/scope-profiles/{profile_id}")
        assert resp.status_code == 403, resp.text
    finally:
        scope_profiles_module.ScopeProfileRepository = original_repo
        scope_profiles_module.get_db = original_get_db
        app.dependency_overrides.pop(resolve_current_actor, None)


def test_route_user_org_role_assign_cross_org_reject():
    """ORG_ADMIN (org-A) 이 org-B 에 user role 부여 시도 → 403.

    admin.py 의 별 endpoint group — `assign_user_org_role` 의 wired guard 검증.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.auth import resolve_current_actor
    from app.api.v1 import admin as admin_module

    actor = _make_actor(role="ORG_ADMIN")
    app.dependency_overrides[resolve_current_actor] = lambda: actor

    user_id = "00000000-0000-0000-0000-0000000000e0"

    # users_repository / organizations_repository / authorization_service 패치.
    fake_user = MagicMock()
    fake_org = MagicMock()
    original_users = admin_module.users_repository
    original_orgs = admin_module.organizations_repository

    users_stub = MagicMock()
    users_stub.get_by_id.return_value = fake_user
    orgs_stub = MagicMock()
    orgs_stub.get_by_id.return_value = fake_org
    admin_module.users_repository = users_stub
    admin_module.organizations_repository = orgs_stub

    # admin.py 가 ResourceRef + authorization_service.authorize 직접 호출 — 통과시킴.
    # actor.role == ORG_ADMIN 이면 admin.write 가 거부될 수 있어서 우회: actor 를 SUPER_ADMIN
    # 으로 두면 cross_org 가 통과되어 검증 무효 → 대신 authorization_service 자체를 패치.
    from app.api.auth import authorization_service as authz_module_attr
    import app.api.v1.admin as admin_v1
    original_authorize = admin_v1.authorization_service.authorize
    admin_v1.authorization_service.authorize = lambda **kw: None

    # ORG_ADMIN 의 user_org_roles 조회 → org-A 만.
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchall.return_value = [{"org_id": "org-A"}]
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    original_get_db = admin_module.get_db
    admin_module.get_db = lambda: conn

    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            f"/api/v1/admin/users/{user_id}/org-roles",
            json={"org_id": "org-B", "role_name": "AUTHOR"},
        )
        assert resp.status_code == 403, resp.text
        # users_repository.assign_org_role 은 호출되어선 안 됨.
        users_stub.assign_org_role.assert_not_called()
    finally:
        admin_module.users_repository = original_users
        admin_module.organizations_repository = original_orgs
        admin_module.get_db = original_get_db
        admin_v1.authorization_service.authorize = original_authorize
        app.dependency_overrides.pop(resolve_current_actor, None)


def test_route_organization_patch_cross_org_reject():
    """ORG_ADMIN (org-A) 이 org-B PATCH 시도 → 403.

    admin.py 의 organization endpoint group — `update_organization` 의 wired guard.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.auth import resolve_current_actor
    from app.api.v1 import admin as admin_module

    actor = _make_actor(role="ORG_ADMIN")
    app.dependency_overrides[resolve_current_actor] = lambda: actor

    target_org = "00000000-0000-0000-0000-0000000000f0"

    original_orgs = admin_module.organizations_repository
    orgs_stub = MagicMock()
    admin_module.organizations_repository = orgs_stub

    import app.api.v1.admin as admin_v1
    original_authorize = admin_v1.authorization_service.authorize
    admin_v1.authorization_service.authorize = lambda **kw: None

    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchall.return_value = [{"org_id": "org-A"}]
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    original_get_db = admin_module.get_db
    admin_module.get_db = lambda: conn

    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/api/v1/admin/organizations/{target_org}",
            json={"name": "should-not-apply"},
        )
        assert resp.status_code == 403, resp.text
        # organizations_repository.update 는 호출되어선 안 됨.
        orgs_stub.update.assert_not_called()
    finally:
        admin_module.organizations_repository = original_orgs
        admin_module.get_db = original_get_db
        admin_v1.authorization_service.authorize = original_authorize
        app.dependency_overrides.pop(resolve_current_actor, None)


def test_route_agent_delete_super_admin_cross_org_audited():
    """SUPER_ADMIN 이 다른 조직 agent 삭제 → 204 + `admin.cross_org_access` emit."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.auth import resolve_current_actor
    from app.api.v1 import scope_profiles as scope_profiles_module

    actor = _make_actor(role="SUPER_ADMIN")
    app.dependency_overrides[resolve_current_actor] = lambda: actor

    target_agent_id = "00000000-0000-0000-0000-0000000000c0"
    fake_agent = MagicMock()
    fake_agent.organization_id = "org-B"

    delete_called: list[bool] = []
    audit_called: list[dict] = []

    class FakeAgentRepo:
        def __init__(self, conn):
            self.conn = conn
        def get_by_id(self, _id):
            return fake_agent
        def delete(self, _id):
            delete_called.append(True)
            return True

    original_repo = scope_profiles_module.AgentRepository
    scope_profiles_module.AgentRepository = FakeAgentRepo

    # SUPER_ADMIN 은 actor_org_ids 결과를 보지 않음 — 임의 conn OK.
    conn = _conn_with_user_org_ids([])
    original_get_db = scope_profiles_module.get_db
    scope_profiles_module.get_db = lambda: conn

    # audit emit 추적 — admin_org_guard 의 emit 경로 + scope_profiles 자체의 emit
    # 모두 동일 audit_emitter 를 사용. 본 회귀는 cross_org_access emit 만 단언.
    from app.utils import admin_org_guard
    original_guard_emit = admin_org_guard.audit_emitter.emit
    admin_org_guard.audit_emitter.emit = lambda **kw: audit_called.append(kw)

    # scope_profiles.delete_agent 자체도 audit emit — 동일 모듈을 가리키므로
    # `admin_org_guard` 의 emitter 만 패치하면 scope_profiles emit 도 잡힌다.
    # (둘 다 `app.audit.emitter.audit_emitter` 싱글톤을 import 한다.)

    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete(f"/api/v1/admin/agents/{target_agent_id}")
        assert resp.status_code == 204, resp.text
        # SUPER_ADMIN 횡단 → cross_org_access emit 1회 이상.
        cross = [e for e in audit_called if e.get("event_type") == "admin.cross_org_access"]
        assert len(cross) >= 1
        # delete 실제 호출 → 통과 경로.
        assert delete_called == [True]
    finally:
        scope_profiles_module.AgentRepository = original_repo
        scope_profiles_module.get_db = original_get_db
        admin_org_guard.audit_emitter.emit = original_guard_emit
        app.dependency_overrides.pop(resolve_current_actor, None)
