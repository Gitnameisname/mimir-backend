"""
A01 Broken Access Control 검증 테스트.

S2 원칙:
  ⑥ Scope Profile 기반 ACL — 코드에 하드코딩된 scope 문자열 금지
  ⑤ AI agent는 사람과 동등한 소비자 — actor_type 기록 필수
  ⑦ 폐쇄망 동등성 — FTS fallback에서도 ACL 동일 적용
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
os.environ.setdefault("JWT_SECRET", "security-test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "security-test-internal")


# ---------------------------------------------------------------------------
# A01-001: Scope 필터가 ScopeProfile 기반으로 동작하는지 확인 (하드코딩 금지 S2 ⑥)
# ---------------------------------------------------------------------------

class TestA01ScopeProfile:
    """Scope Profile 기반 ACL 검증."""

    def test_scope_filter_module_exists(self):
        """A01-001: scope_filter 모듈이 존재하고 apply_scope_filter 함수가 있다."""
        from app.mcp.scope_filter import apply_scope_filter
        assert callable(apply_scope_filter)

    def test_scope_filter_no_hardcoded_scope_strings(self):
        """A01-002: scope_filter.py 소스 코드에 하드코딩된 scope 문자열이 없다 (S2 ⑥).

        'if scope == "team"' 같은 패턴이 없어야 한다.
        """
        scope_filter_path = ROOT / "backend/app/mcp/scope_filter.py"
        source = scope_filter_path.read_text(encoding="utf-8")

        import re
        hardcoded_scope_patterns = [
            r'scope\s*==\s*["\']team["\']',
            r'scope\s*==\s*["\']personal["\']',
            r'scope\s*==\s*["\']organization["\']',
            r'scope\s*==\s*["\']public["\']',
        ]
        for pattern in hardcoded_scope_patterns:
            matches = re.findall(pattern, source)
            assert not matches, (
                f"하드코딩된 scope 문자열 발견 (S2 ⑥ 위반): {pattern} → {matches}"
            )

    def test_filter_expression_module_exists(self):
        """A01-003: filter_expression 모듈이 존재하고 parameterized 처리를 한다."""
        from app.services.filter_expression import build_sql_filter, parse_filter_expression
        assert callable(build_sql_filter)
        assert callable(parse_filter_expression)

    def test_scope_profile_repository_exists(self):
        """A01-004: ScopeProfileRepository가 존재한다."""
        from app.repositories.scope_profile_repository import ScopeProfileRepository
        assert ScopeProfileRepository is not None

    def test_no_hardcoded_scope_in_search_service(self):
        """A01-005: search_service.py에 하드코딩된 scope 분기가 없다 (S2 ⑥)."""
        search_path = ROOT / "backend/app/services/search_service.py"
        if not search_path.exists():
            pytest.skip("search_service.py 없음 — 다른 경로로 검색 처리됨")

        import re
        source = search_path.read_text(encoding="utf-8")
        hardcoded = re.findall(r'if.*scope.*==.*["\'](?:team|personal|organization|public)["\']', source)
        assert not hardcoded, f"search_service에서 하드코딩된 scope 분기 발견: {hardcoded}"


# ---------------------------------------------------------------------------
# A01-006~009: Agent actor_type 기록 (S2 ⑤)
# ---------------------------------------------------------------------------

class TestA01AuditActorType:
    """감사 로그 actor_type 기록 검증."""

    def test_audit_emitter_has_actor_type_param(self):
        """A01-006: AuditEmitter.emit()이 actor_type 파라미터를 지원한다."""
        import inspect
        from app.audit.emitter import AuditEmitter

        sig = inspect.signature(AuditEmitter.emit)
        assert "actor_type" in sig.parameters, "actor_type 파라미터 누락 (S2 ⑤ 위반)"

    def test_audit_emitter_records_agent_type(self):
        """A01-007: emit()이 actor_type='agent'를 로그에 포함한다."""
        from app.audit.emitter import AuditEmitter
        emitter = AuditEmitter()

        with patch.object(emitter, "_persist") as mock_persist:
            with patch("app.audit.emitter._audit_logger") as mock_logger:
                emitter.emit(
                    event_type="document.search",
                    action="document.search",
                    actor_id="agent-001",
                    actor_type="agent",
                    resource_type="document",
                    result="success",
                )
                # structured log에 actor_type 포함 확인
                logged_event = mock_logger.info.call_args[0][1]
                assert logged_event.get("actor_type") == "agent", (
                    "감사 로그에 actor_type='agent' 기록 안 됨 (S2 ⑤ 위반)"
                )

    def test_audit_emitter_records_user_type(self):
        """A01-008: emit()이 actor_type='user'를 로그에 포함한다."""
        from app.audit.emitter import AuditEmitter
        emitter = AuditEmitter()

        with patch.object(emitter, "_persist"):
            with patch("app.audit.emitter._audit_logger") as mock_logger:
                emitter.emit(
                    event_type="document.search",
                    action="document.search",
                    actor_id="user-001",
                    actor_type="user",
                    resource_type="document",
                    result="success",
                )
                logged_event = mock_logger.info.call_args[0][1]
                assert logged_event.get("actor_type") == "user"

    def test_emit_for_actor_maps_agent_type(self):
        """A01-009: emit_for_actor()가 SERVICE 타입 actor를 'agent'로 변환한다."""
        from app.audit.emitter import AuditEmitter
        emitter = AuditEmitter()

        mock_actor = MagicMock()
        mock_actor.is_authenticated = True
        mock_actor.actor_id = "service-001"
        mock_actor.role = "VIEWER"

        mock_actor_type = MagicMock()
        mock_actor_type.value = "service"
        mock_actor.actor_type = mock_actor_type

        with patch.object(emitter, "_persist"):
            with patch("app.audit.emitter._audit_logger") as mock_logger:
                emitter.emit_for_actor(
                    event_type="test",
                    action="test",
                    actor=mock_actor,
                    resource_type="document",
                )
                logged_event = mock_logger.info.call_args[0][1]
                assert logged_event.get("actor_type") == "agent", (
                    "SERVICE 타입 actor가 'agent'로 매핑되지 않음"
                )


# ---------------------------------------------------------------------------
# A01-010: Agent Proposal은 proposed 상태로만 저장 (FG5.1)
# ---------------------------------------------------------------------------

class TestA01AgentProposalScope:
    """에이전트 쓰기 작업의 proposed 전용 제약 검증."""

    def test_agent_proposal_service_exists(self):
        """A01-010: AgentProposalService가 존재한다."""
        from app.services.agent_proposal_service import AgentProposalService
        assert AgentProposalService is not None

    def test_agent_proposal_sets_proposed_status(self):
        """A01-011: AgentProposalService.propose_draft()가 proposed 상태를 강제한다."""
        source_path = ROOT / "backend/app/services/agent_proposal_service.py"
        source = source_path.read_text(encoding="utf-8")

        assert "'proposed'" in source or '"proposed"' in source, (
            "propose_draft에서 'proposed' 상태 강제 코드 없음"
        )

    def test_agent_kill_switch_exists(self):
        """A01-012: Agent kill switch API가 구현되어 있다."""
        scope_profiles_path = ROOT / "backend/app/api/v1/scope_profiles.py"
        source = scope_profiles_path.read_text(encoding="utf-8")

        assert "kill_switch" in source, "Kill switch 구현 없음"
        assert "enable_kill_switch" in source or "activate_kill_switch" in source, (
            "Kill switch 활성화 함수 없음"
        )

    def test_kill_switch_audit_logged(self):
        """A01-013: Kill switch 발동 시 감사 로그가 기록된다."""
        scope_profiles_path = ROOT / "backend/app/api/v1/scope_profiles.py"
        source = scope_profiles_path.read_text(encoding="utf-8")

        assert "kill_switch_activated" in source or "kill_switch.activate" in source, (
            "Kill switch 활성화 이벤트가 감사 로그에 기록되지 않음"
        )


# ---------------------------------------------------------------------------
# A01-014: 폐쇄망 FTS fallback ACL (S2 ⑦)
# ---------------------------------------------------------------------------

class TestA01FtsAcl:
    """FTS fallback 경로에서도 ACL 적용 검증."""

    def test_fts_retriever_does_not_bypass_acl(self):
        """A01-014: FTS retriever가 scope 필터를 포함해야 한다 (S2 ⑦)."""
        fts_path = ROOT / "backend/app/services/retrieval/fts_retriever.py"
        if not fts_path.exists():
            pytest.skip("fts_retriever.py 없음")

        source = fts_path.read_text(encoding="utf-8")
        # scope_profile 또는 acl_filter 또는 scope_name 사용 확인
        has_acl = any(
            kw in source for kw in [
                "scope_profile", "acl_filter", "scope_name",
                "apply_scope_filter", "access_context",
                "build_chunk_acl_clause", "accessible_org_ids", "accessible_user_ids",
            ]
        )
        assert has_acl, "FTS retriever에 ACL 적용 코드 없음 (S2 ⑦ 위반 위험)"

    def test_search_service_has_fts_fallback(self):
        """A01-015: search_service가 FTS fallback을 지원한다."""
        retrieval_dir = ROOT / "backend/app/services/retrieval"
        assert retrieval_dir.exists(), "retrieval 서비스 디렉토리 없음"

        fts_files = list(retrieval_dir.glob("*fts*"))
        assert fts_files, "FTS retriever 파일 없음"
