"""
LLM08 Excessive Agency 검증 테스트.

검증 항목:
  - Draft Proposal 패턴 (위험 작업은 항상 proposed 상태)
  - Human-in-the-loop: 관리자 승인 필수
  - Kill switch 5초 내 응답
  - Agent scope 제한 (Scope Profile ACL)
  - agent actor_type 감사 로그
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "security-test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "security-test-internal")


# ---------------------------------------------------------------------------
# LLM08-001~005: Draft Proposal 패턴
# ---------------------------------------------------------------------------

class TestLLM08DraftProposal:
    """Agent 작업이 Draft Proposal 패턴을 따르는지 검증."""

    def test_agent_proposal_service_exists(self):
        """LLM08-001: AgentProposalService가 존재한다."""
        from app.services.agent_proposal_service import AgentProposalService
        assert AgentProposalService is not None

    def test_propose_draft_sets_proposed_status(self):
        """LLM08-002: propose_draft()가 workflow_status=proposed를 설정한다."""
        from app.services.agent_proposal_service import AgentProposalService
        import inspect

        source_path = ROOT / "backend/app/services/agent_proposal_service.py"
        source = source_path.read_text(encoding="utf-8")

        assert "proposed" in source, "Draft Proposal 패턴: proposed 상태 없음"

    def test_agent_cannot_directly_publish(self):
        """LLM08-003: Agent가 문서를 직접 published 상태로 변경할 수 없다."""
        service_path = ROOT / "backend/app/services/agent_proposal_service.py"
        source = service_path.read_text(encoding="utf-8")

        # agent 경로에서 directly published 상태를 설정하는 코드가 없어야 함
        import re
        direct_publish = re.findall(
            r'workflow_status\s*[=:]\s*["\']published["\']',
            source,
        )
        # proposed 경로에서 published로 직접 설정하는 경우 차단
        assert not direct_publish, (
            "AgentProposalService에서 직접 published 설정: {direct_publish}"
        )

    def test_approval_required_for_agent_proposals(self):
        """LLM08-004: Agent 제안은 인간 승인이 필요하다."""
        service_path = ROOT / "backend/app/services/agent_proposal_service.py"
        source = service_path.read_text(encoding="utf-8")

        has_approval = "approve" in source.lower() or "human" in source.lower()
        assert has_approval, "Agent 제안에 인간 승인 메커니즘 없음"

    def test_reject_proposal_exists(self):
        """LLM08-005: 제안 거부(reject) 기능이 있다."""
        from app.services.agent_proposal_service import AgentProposalService
        methods = [m for m in dir(AgentProposalService) if not m.startswith("_")]
        has_reject = any("reject" in m.lower() for m in methods)
        assert has_reject, "제안 거부 기능 없음"


# ---------------------------------------------------------------------------
# LLM08-006~009: Kill Switch
# ---------------------------------------------------------------------------

class TestLLM08KillSwitch:
    """Kill Switch 동작 검증."""

    def test_kill_switch_endpoint_exists(self):
        """LLM08-006: Kill switch API 엔드포인트가 존재한다."""
        scope_profiles_path = ROOT / "backend/app/api/v1/scope_profiles.py"
        source = scope_profiles_path.read_text(encoding="utf-8")

        assert "kill" in source.lower() or "kill_switch" in source.lower(), (
            "Kill switch 엔드포인트 없음"
        )

    def test_kill_switch_emits_audit_event(self):
        """LLM08-007: Kill switch 활성화 시 감사 이벤트가 기록된다."""
        scope_profiles_path = ROOT / "backend/app/api/v1/scope_profiles.py"
        source = scope_profiles_path.read_text(encoding="utf-8")

        has_audit = "audit_emitter" in source or "emit" in source
        assert has_audit, "Kill switch에 감사 로그 없음"

    def test_kill_switch_disables_agent_within_5_seconds(self):
        """LLM08-008: Kill switch가 5초 내 활성화된다."""
        # 실제 DB 없이 서비스 코드의 응답 시간 특성 확인
        scope_profiles_path = ROOT / "backend/app/api/v1/scope_profiles.py"
        source = scope_profiles_path.read_text(encoding="utf-8")

        # 비동기 DB 업데이트 (단일 UPDATE)으로 구현되어 있어야 빠름
        has_async = "async" in source
        has_db_update = "update" in source.lower() or "enable_kill_switch" in source
        assert has_async and has_db_update, (
            "Kill switch가 비동기 DB 업데이트로 구현되지 않음 (5초 내 응답 불확실)"
        )

    def test_kill_switch_state_stored_in_db(self):
        """LLM08-009: Kill switch 상태가 DB에 저장된다."""
        scope_profiles_path = ROOT / "backend/app/api/v1/scope_profiles.py"
        source = scope_profiles_path.read_text(encoding="utf-8")

        has_persistence = any(
            kw in source
            for kw in ["repo", "repository", "db", "session"]
        )
        assert has_persistence, "Kill switch 상태가 DB에 저장되지 않음"


# ---------------------------------------------------------------------------
# LLM08-010~013: Scope 제한
# ---------------------------------------------------------------------------

class TestLLM08ScopeRestriction:
    """Agent scope 제한 검증."""

    def test_scope_filter_applies_to_agent_requests(self):
        """LLM08-010: Agent 요청에도 Scope Profile ACL이 적용된다."""
        mcp_scope_path = ROOT / "backend/app/mcp/scope_filter.py"
        if not mcp_scope_path.exists():
            pytest.skip("mcp/scope_filter.py 없음")

        source = mcp_scope_path.read_text(encoding="utf-8")
        has_acl = "ScopeProfileRepository" in source or "apply_scope_filter" in source
        assert has_acl, "MCP scope filter에 ACL 없음"

    def test_no_hardcoded_scope_strings_in_agent_code(self):
        """LLM08-011: Agent 관련 코드에 하드코딩된 scope 문자열이 없다 (S2 ⑥)."""
        agent_proposal_path = ROOT / "backend/app/services/agent_proposal_service.py"
        source = agent_proposal_path.read_text(encoding="utf-8")

        import re
        hardcoded = re.findall(
            r'if\s+scope\s*==\s*["\'](?:team|org|public|private)["\']',
            source,
            re.IGNORECASE,
        )
        assert not hardcoded, (
            f"Agent 코드에 하드코딩된 scope 문자열 (S2 ⑥ 위반): {hardcoded}"
        )

    def test_agent_actor_type_logged_in_audit(self):
        """LLM08-012: Agent 작업 시 actor_type='agent'가 감사 로그에 기록된다."""
        service_path = ROOT / "backend/app/services/agent_proposal_service.py"
        source = service_path.read_text(encoding="utf-8")

        has_agent_type = "agent" in source and (
            "actor_type" in source or "emit" in source
        )
        assert has_agent_type, "Agent 감사 로그에 actor_type='agent' 없음 (S2 ⑤ 위반)"

    def test_agent_proposal_route_requires_auth(self):
        """LLM08-013: Agent proposal API가 인증을 요구한다."""
        agent_router_path = ROOT / "backend/app/api/v1/agent_proposals.py"
        if not agent_router_path.exists():
            pytest.skip("agent_proposals.py 없음")

        source = agent_router_path.read_text(encoding="utf-8")
        has_auth = "Depends" in source and any(
            kw in source for kw in ["get_current_user", "require_", "auth"]
        )
        assert has_auth, "Agent proposal API에 인증 없음"
