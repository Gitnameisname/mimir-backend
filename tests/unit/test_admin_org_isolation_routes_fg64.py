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
