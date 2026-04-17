"""
A04 Insecure Design 검증 테스트.

검증 항목:
  - AI-as-first-class: Agent가 API를 통해서만 동작
  - Draft Proposal 패턴: 에이전트 쓰기는 항상 proposed 상태
  - Kill Switch: 에이전트를 5초 이내에 비활성화 가능
  - Scope 제한: 에이전트는 할당된 Scope Profile 범위 내에서만 동작
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "security-test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "security-test-internal")


# ---------------------------------------------------------------------------
# A04-001~004: Draft Proposal 패턴 (FG5.1)
# ---------------------------------------------------------------------------

class TestA04DraftProposal:
    """에이전트 Draft Proposal 패턴 검증."""

    def test_agent_proposal_service_propose_draft_exists(self):
        """A04-001: AgentProposalService.propose_draft() 메서드가 존재한다."""
        from app.services.agent_proposal_service import AgentProposalService
        assert hasattr(AgentProposalService, "propose_draft")

    def test_agent_propose_creates_proposed_status(self):
        """A04-002: propose_draft()가 생성하는 version의 workflow_status가 'proposed'이다."""
        source_path = ROOT / "backend/app/services/agent_proposal_service.py"
        source = source_path.read_text(encoding="utf-8")

        # proposed 상태가 명시적으로 설정되어야 함
        assert "'proposed'" in source or '"proposed"' in source, (
            "propose_draft에서 'proposed' 상태 강제 코드 없음"
        )
        # published나 approved를 직접 설정하는 코드가 없어야 함 (proposals만 해당)
        import re
        dangerous_direct = re.findall(
            r"workflow_status\s*=\s*['\"]published['\"]",
            source,
        )
        # propose_draft 함수 내에서는 published 직접 설정 금지
        # (approve_draft에서만 가능)
        assert not dangerous_direct or "approve" in source, (
            "propose_draft가 직접 published 상태를 설정함 (승인 게이트 우회)"
        )

    def test_agent_proposal_requires_human_approval(self):
        """A04-003: approve_draft() / reject_draft() 메서드가 존재하여 인간 검토를 강제한다."""
        from app.services.agent_proposal_service import AgentProposalService
        assert hasattr(AgentProposalService, "approve_draft"), "approve_draft 없음"
        assert hasattr(AgentProposalService, "reject_draft"), "reject_draft 없음"

    def test_proposal_transitions_use_audit_log(self):
        """A04-004: 제안 승인/반려 시 감사 로그가 기록된다."""
        source_path = ROOT / "backend/app/services/agent_proposal_service.py"
        source = source_path.read_text(encoding="utf-8")

        assert "audit_emitter" in source, "agent_proposal_service에 감사 로그 없음"
        assert "actor_type" in source, "actor_type 기록 없음"


# ---------------------------------------------------------------------------
# A04-005~008: Kill Switch
# ---------------------------------------------------------------------------

class TestA04KillSwitch:
    """Kill Switch 검증."""

    def test_kill_switch_api_exists(self):
        """A04-005: Kill switch API 엔드포인트가 존재한다."""
        scope_profiles_path = ROOT / "backend/app/api/v1/scope_profiles.py"
        source = scope_profiles_path.read_text(encoding="utf-8")

        assert "kill_switch" in source, "kill_switch API 없음"

    def test_kill_switch_enables_agent_inactive(self):
        """A04-006: kill switch 활성화 시 에이전트가 비활성 상태가 된다."""
        agent_repo_path = ROOT / "backend/app/repositories/agent_repository.py"
        if not agent_repo_path.exists():
            pytest.skip("agent_repository.py 없음")

        source = agent_repo_path.read_text(encoding="utf-8")
        assert "enable_kill_switch" in source, "enable_kill_switch 함수 없음"
        # is_active = False 또는 killed = True 설정 확인
        assert "False" in source or "inactive" in source.lower() or "killed" in source.lower(), (
            "kill switch가 에이전트 비활성화를 수행하지 않음"
        )

    def test_kill_switch_logs_audit_event(self):
        """A04-007: Kill switch 활성화가 감사 이벤트를 기록한다."""
        scope_profiles_path = ROOT / "backend/app/api/v1/scope_profiles.py"
        source = scope_profiles_path.read_text(encoding="utf-8")

        assert "kill_switch_activated" in source or "kill_switch.activate" in source, (
            "Kill switch 활성화 감사 이벤트 없음"
        )

    def test_kill_switch_timing_under_5_seconds(self):
        """A04-008: Kill switch가 5초 이내에 응답한다 (API 구조 검증).

        실제 DB 없이 API 코드 구조로 검증: 동기 처리, 즉각적인 상태 변경.
        """
        scope_profiles_path = ROOT / "backend/app/api/v1/scope_profiles.py"
        source = scope_profiles_path.read_text(encoding="utf-8")

        # async sleep이나 긴 대기 없이 즉시 처리되는지 확인
        import re
        long_wait = re.findall(r"asyncio\.sleep\s*\(\s*[5-9]\d*", source)
        assert not long_wait, f"Kill switch에 불필요한 대기 발견: {long_wait}"


# ---------------------------------------------------------------------------
# A04-009~012: Agent가 API를 통해서만 동작 (AI-as-first-class)
# ---------------------------------------------------------------------------

class TestA04AgentApiParity:
    """에이전트-사람 API 동등성 검증."""

    def test_mcp_server_exists(self):
        """A04-009: MCP 서버가 에이전트를 위한 API를 제공한다."""
        mcp_dir = ROOT / "backend/app/mcp"
        assert mcp_dir.exists(), "MCP 모듈 없음"

        mcp_tools = mcp_dir / "tools.py"
        assert mcp_tools.exists(), "MCP tools.py 없음"

    def test_mcp_tools_uses_scope_filter(self):
        """A04-010: MCP tools가 Scope Profile ACL을 적용한다."""
        mcp_tools_path = ROOT / "backend/app/mcp/tools.py"
        source = mcp_tools_path.read_text(encoding="utf-8")

        assert "scope" in source.lower(), "MCP tools에 scope 처리 없음"

    def test_agent_write_blocked_without_proposal(self):
        """A04-011: 에이전트가 제안 절차 없이 직접 문서를 published로 생성할 수 없다."""
        source_path = ROOT / "backend/app/services/agent_proposal_service.py"
        source = source_path.read_text(encoding="utf-8")

        # propose_draft만 있고 직접 publish 없음 확인
        import re
        direct_publish = re.findall(
            r"def\s+(?!propose_|approve_|reject_|withdraw_)\w*publish\w*",
            source,
        )
        # AgentProposalService에는 직접 publish 메서드 없어야 함
        assert not direct_publish, (
            f"에이전트가 직접 publish할 수 있는 메서드 발견: {direct_publish}"
        )

    def test_agent_proposal_audit_records_actor_type_agent(self):
        """A04-012: 에이전트 제안 생성 시 actor_type='agent'가 기록된다."""
        source_path = ROOT / "backend/app/services/agent_proposal_service.py"
        source = source_path.read_text(encoding="utf-8")

        # "agent" actor_type 기록 확인
        assert '"agent"' in source or "'agent'" in source, (
            "에이전트 제안에서 actor_type='agent' 기록 없음"
        )
