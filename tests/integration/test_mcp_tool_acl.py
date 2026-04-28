"""
S3 Phase 4 FG 4-0 §2.1.6.f: MCP tool-level ACL 회귀 테스트.

ScopeProfile.allowed_tools + AgentPrincipal.can_call_tool 게이트 검증.

R2 (Phase 4 개발계획서 §1.2): ACL 단일 결정점 — Scope Profile 만이 ACL 을 결정한다.
본 테스트는 task3-3.md §[129,223–225,318] 의 흡수 종결 (S3 P3 정적 리뷰 P1) 회귀.

6 시나리오 (task4-0 §2.1.6.f):
  1. 빈 allowed_tools 인 프로파일 → 모든 도구 거부 (default-deny)
  2. allowed_tools=["read_annotations"] → read_annotations 통과
  3. allowed_tools=["search_documents"] → read_annotations 거부 + search_documents 통과
  4. tools/list per-actor 필터 (다른 도구 비노출)
  5. actor_type=user / system → 본 게이트 미적용
  6. allowed_tools 와 manifest.status=not_exposed 동시 적용 — manifest 우선

모든 테스트는 mock 기반 — 실 DB 불필요.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "mcp-acl-test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal-secret")


# ===========================================================================
# 헬퍼 — 가짜 ScopeProfile / ActorContext
# ===========================================================================


def _fake_profile(allowed_tools: list[str]) -> "ScopeProfile":  # noqa: F821
    """ScopeProfile dataclass 인스턴스 (allowed_tools 만 의미 있음)."""
    from datetime import datetime
    from app.models.scope_profile import ScopeProfile, ScopeProfileSettings

    return ScopeProfile(
        id="00000000-0000-0000-0000-000000000001",
        name="test-profile",
        description=None,
        organization_id=None,
        created_at=datetime(2026, 4, 28),
        updated_at=datetime(2026, 4, 28),
        scopes=[],
        settings=ScopeProfileSettings(),
        allowed_tools=list(allowed_tools),
    )


def _agent_actor(scope_profile_id: str = "00000000-0000-0000-0000-000000000001") -> "ActorContext":  # noqa: F821
    from app.api.auth.models import ActorContext, ActorType, AuthMethod

    return ActorContext(
        actor_type=ActorType.AGENT,
        actor_id="agent-1",
        is_authenticated=True,
        auth_method=AuthMethod.API_KEY,
        tenant_id=None,
        role=None,
        agent_id="agent-1",
        scope_profile_id=scope_profile_id,
    )


def _user_actor() -> "ActorContext":  # noqa: F821
    from app.api.auth.models import ActorContext, ActorType, AuthMethod

    return ActorContext(
        actor_type=ActorType.USER,
        actor_id="user-1",
        is_authenticated=True,
        auth_method=AuthMethod.SESSION,
        tenant_id=None,
        role="AUTHOR",
    )


def _system_actor() -> "ActorContext":  # noqa: F821
    from app.api.auth.models import ActorContext, ActorType, AuthMethod

    return ActorContext(
        actor_type=ActorType.SERVICE,
        actor_id="svc-1",
        is_authenticated=True,
        auth_method=AuthMethod.INTERNAL_SERVICE,
        tenant_id=None,
        role=None,
    )


def _patch_repo_get(profile):
    """ScopeProfileRepository.get_by_id 를 mock 으로 교체한다.

    ActorContext.can_call_tool 이 자체적으로 `get_db()` + Repository 인스턴스를
    생성하므로, get_db 와 Repository.get_by_id 모두 patch.
    """
    from contextlib import contextmanager

    @contextmanager
    def _fake_get_db():
        yield MagicMock()  # conn — Repository 가 사용하지 않게 됨 (get_by_id 가 mock)

    return [
        patch("app.api.auth.models.get_db", _fake_get_db, create=True),
        patch(
            "app.repositories.scope_profile_repository.ScopeProfileRepository.get_by_id",
            return_value=profile,
        ),
    ]


# ===========================================================================
# Scenario 1: default-deny
# ===========================================================================


class TestScenario1DefaultDeny:
    """빈 allowed_tools 인 프로파일은 모든 MCP 도구 호출을 거부한다."""

    def test_can_call_tool_returns_false_for_empty_allowed(self):
        actor = _agent_actor()
        profile = _fake_profile([])

        patches = _patch_repo_get(profile)
        for p in patches:
            p.start()
        try:
            assert actor.can_call_tool("search_documents") is False
            assert actor.can_call_tool("read_annotations") is False
            assert actor.can_call_tool("fetch_node") is False
        finally:
            for p in patches:
                p.stop()

    def test_check_tool_allowed_raises_403_for_empty(self):
        from app.mcp.tools import _check_tool_allowed
        from app.mcp.errors import MCPError, MCPErrorCode

        actor = _agent_actor()
        profile = _fake_profile([])

        patches = _patch_repo_get(profile)
        for p in patches:
            p.start()
        try:
            with pytest.raises(MCPError) as exc_info:
                _check_tool_allowed(actor, "search_documents")
            assert exc_info.value.code == MCPErrorCode.UNAUTHORIZED
            assert exc_info.value.http_status == 403
        finally:
            for p in patches:
                p.stop()


# ===========================================================================
# Scenario 2: allowed_tools=["read_annotations"] → read_annotations 통과
# ===========================================================================


class TestScenario2SingleToolAllowed:
    def test_allowed_tool_passes(self):
        actor = _agent_actor()
        profile = _fake_profile(["read_annotations"])

        patches = _patch_repo_get(profile)
        for p in patches:
            p.start()
        try:
            assert actor.can_call_tool("read_annotations") is True
        finally:
            for p in patches:
                p.stop()


# ===========================================================================
# Scenario 3: 도구 격리
# ===========================================================================


class TestScenario3ToolIsolation:
    """allowed_tools=["search_documents"] 만 있는 프로파일은 다른 도구 거부."""

    def test_search_passes_annotations_denied(self):
        actor = _agent_actor()
        profile = _fake_profile(["search_documents"])

        patches = _patch_repo_get(profile)
        for p in patches:
            p.start()
        try:
            assert actor.can_call_tool("search_documents") is True
            assert actor.can_call_tool("read_annotations") is False
            assert actor.can_call_tool("fetch_node") is False
            assert actor.can_call_tool("verify_citation") is False
        finally:
            for p in patches:
                p.stop()


# ===========================================================================
# Scenario 4: tools/list per-actor 필터
# ===========================================================================


class TestScenario4PerActorFilter:
    """tools/list 응답이 ScopeProfile.allowed_tools 로 필터링된다.

    구조 검증 — `_agent_allowed_tools` 헬퍼가 actor 의 ScopeProfile.allowed_tools
    set 을 반환해야 한다.
    """

    def test_agent_allowed_tools_returns_set(self):
        from app.api.v1.mcp_router import _agent_allowed_tools

        actor = _agent_actor()
        profile = _fake_profile(["search_documents", "read_annotations"])

        patches = _patch_repo_get(profile)
        for p in patches:
            p.start()
        try:
            allowed = _agent_allowed_tools(actor)
            assert allowed == {"search_documents", "read_annotations"}
        finally:
            for p in patches:
                p.stop()

    def test_agent_allowed_tools_empty_set_for_default_deny(self):
        from app.api.v1.mcp_router import _agent_allowed_tools

        actor = _agent_actor()
        profile = _fake_profile([])

        patches = _patch_repo_get(profile)
        for p in patches:
            p.start()
        try:
            allowed = _agent_allowed_tools(actor)
            assert allowed == set()
        finally:
            for p in patches:
                p.stop()

    def test_agent_allowed_tools_returns_none_when_no_profile_id(self):
        """scope_profile_id 없는 에이전트는 필터 미적용 (None 반환)."""
        from app.api.v1.mcp_router import _agent_allowed_tools

        actor = _agent_actor(scope_profile_id="")
        actor.scope_profile_id = None
        allowed = _agent_allowed_tools(actor)
        assert allowed is None


# ===========================================================================
# Scenario 5: user / system actor 는 본 게이트 비대상
# ===========================================================================


class TestScenario5NonAgentBypass:
    """user / system / anonymous actor 는 본 게이트가 항상 통과시킨다.

    인증·권한은 별 게이트가 처리. 본 게이트는 에이전트 전용.
    """

    def test_user_actor_passes_without_profile_lookup(self):
        actor = _user_actor()
        # user 는 ScopeProfile 조회 자체가 발생하지 않아야 함 — Repository mock 없이도 통과
        assert actor.can_call_tool("search_documents") is True
        assert actor.can_call_tool("read_annotations") is True
        # 임의의 도구 이름도 통과 (게이트 비대상)
        assert actor.can_call_tool("__nonexistent_tool__") is True

    def test_system_actor_passes(self):
        actor = _system_actor()
        assert actor.can_call_tool("search_documents") is True

    def test_check_tool_allowed_accepts_user_actor(self):
        from app.mcp.tools import _check_tool_allowed

        actor = _user_actor()
        # 예외 없이 반환
        _check_tool_allowed(actor, "search_documents")


# ===========================================================================
# Scenario 6: manifest 우선 (manifest.status=not_exposed → allowed_tools 무관)
# ===========================================================================


class TestScenario6ManifestPrecedence:
    """manifest 의 not_exposed/forbidden 도구는 allowed_tools 와 무관하게 거부된다.

    `_CURATED_TOOLS` 가 mcp_exposed_tool_schemas() 기반이므로, 그 외 도구는
    `tools/call` dispatcher 의 첫 게이트 (`tool_name not in _CURATED_TOOLS`) 에서
    INVALID_REQUEST 로 거부된다 — allowed_tools 검사 도달 전.
    """

    def test_not_exposed_tool_not_in_curated(self):
        from app.api.v1.mcp_router import _CURATED_TOOLS

        # 운영 정책상 publish_document / reindex_document / change_schema /
        # delete_document 는 TOOL_SCHEMAS 에 등재 안 됨 → _CURATED_TOOLS 에 없음
        assert "publish_document" not in _CURATED_TOOLS
        assert "reindex_document" not in _CURATED_TOOLS
        assert "change_schema" not in _CURATED_TOOLS
        assert "delete_document" not in _CURATED_TOOLS

    def test_curated_only_includes_l0_l1_l2_enabled(self):
        """노출 도구는 L0 / L1 / L2 (FG 4-6 save_draft) 만. L3/L4 영구 제외 (R1)."""
        from app.api.v1.mcp_router import _CURATED_TOOLS
        from app.schemas.mcp import TOOL_SCHEMAS

        for name in _CURATED_TOOLS:
            schema = next(s for s in TOOL_SCHEMAS if s["name"] == name)
            assert schema["risk_tier"] in {"L0", "L1", "L2"}
            assert schema["maturity"] != "forbidden"
            assert schema["status"] == "enabled"


# ===========================================================================
# 추가: Repository / Schema 검증
# ===========================================================================


class TestRepositoryValidation:
    """ScopeProfileRepository 가 known_tool_names() 외 도구 이름을 거부한다."""

    def test_validate_rejects_unknown_tool(self):
        from app.repositories.scope_profile_repository import _allowed_tools_validate

        with pytest.raises(ValueError) as exc_info:
            _allowed_tools_validate(["nonexistent_evil_tool"])
        assert "nonexistent_evil_tool" in str(exc_info.value)

    def test_validate_accepts_known_tools(self):
        from app.repositories.scope_profile_repository import _allowed_tools_validate

        result = _allowed_tools_validate(["search_documents", "read_annotations"])
        # 정렬된 + 중복 제거된 결과
        assert result == ["read_annotations", "search_documents"]

    def test_validate_dedupes_and_sorts(self):
        from app.repositories.scope_profile_repository import _allowed_tools_validate

        result = _allowed_tools_validate(["search_documents", "search_documents", "fetch_node"])
        assert result == ["fetch_node", "search_documents"]

    def test_known_tool_names_matches_tool_schemas(self):
        from app.schemas.mcp import TOOL_SCHEMAS, known_tool_names

        assert known_tool_names() == frozenset(s["name"] for s in TOOL_SCHEMAS)


# ===========================================================================
# 추가: ScopeProfile dataclass / Schema 정합
# ===========================================================================


class TestScopeProfileDataclassConsistency:
    def test_scope_profile_has_allowed_tools_field(self):
        from dataclasses import fields
        from app.models.scope_profile import ScopeProfile

        field_names = {f.name for f in fields(ScopeProfile)}
        assert "allowed_tools" in field_names

    def test_scope_profile_response_schema_has_allowed_tools(self):
        from app.schemas.agent import ScopeProfileResponse

        assert "allowed_tools" in ScopeProfileResponse.model_fields

    def test_scope_profile_create_schema_has_allowed_tools(self):
        from app.schemas.agent import ScopeProfileCreate

        assert "allowed_tools" in ScopeProfileCreate.model_fields

    def test_scope_profile_update_schema_has_allowed_tools(self):
        from app.schemas.agent import ScopeProfileUpdate

        assert "allowed_tools" in ScopeProfileUpdate.model_fields
