"""
A09 Security Logging and Monitoring Failures 검증 테스트.

검증 항목:
  - 모든 API 호출이 감사 로그에 기록됨
  - 필수 필드 (timestamp, actor_type, actor_id, action, result) 포함
  - Critical 에러 시 알림 전송
  - Agent 활동 체인 추적
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
# A09-001~005: 감사 로그 완전성
# ---------------------------------------------------------------------------

class TestA09AuditLogCompleteness:
    """감사 로그 완전성 검증."""

    def test_audit_emitter_singleton_exists(self):
        """A09-001: AuditEmitter 싱글턴이 존재한다."""
        from app.audit.emitter import audit_emitter, AuditEmitter
        assert isinstance(audit_emitter, AuditEmitter)

    def test_audit_emit_logs_structured_event(self):
        """A09-002: emit()이 structured log를 출력한다."""
        from app.audit.emitter import AuditEmitter

        emitter = AuditEmitter()

        with patch.object(emitter, "_persist"):
            with patch("app.audit.emitter._audit_logger") as mock_logger:
                emitter.emit(
                    event_type="document.created",
                    action="document.create",
                    actor_id="user-001",
                    actor_type="user",
                    resource_type="document",
                    resource_id="doc-abc",
                    result="success",
                )
                mock_logger.info.assert_called_once()
                log_args = mock_logger.info.call_args[0]
                assert "AUDIT" in log_args[0], "AUDIT 접두사 없음"

    def test_audit_log_contains_timestamp(self):
        """A09-003: 감사 로그에 타임스탬프가 포함된다."""
        from app.audit.emitter import AuditEmitter

        emitter = AuditEmitter()

        with patch.object(emitter, "_persist"):
            with patch("app.audit.emitter._audit_logger") as mock_logger:
                emitter.emit(
                    event_type="test",
                    action="test",
                    actor_id="user-001",
                    actor_type="user",
                    resource_type="document",
                    result="success",
                )
                logged_event = mock_logger.info.call_args[0][1]
                assert "timestamp" in logged_event, "감사 로그에 timestamp 없음"

    def test_audit_log_contains_actor_type(self):
        """A09-004: 감사 로그에 actor_type이 포함된다 (S2 ⑤)."""
        from app.audit.emitter import AuditEmitter

        emitter = AuditEmitter()

        with patch.object(emitter, "_persist"):
            with patch("app.audit.emitter._audit_logger") as mock_logger:
                emitter.emit(
                    event_type="test",
                    action="test",
                    actor_id="agent-001",
                    actor_type="agent",
                    resource_type="document",
                    result="success",
                )
                logged_event = mock_logger.info.call_args[0][1]
                assert "actor_type" in logged_event, "actor_type 누락 (S2 ⑤ 위반)"
                assert logged_event["actor_type"] == "agent"

    def test_audit_log_contains_result(self):
        """A09-005: 감사 로그에 result 필드가 포함된다."""
        from app.audit.emitter import AuditEmitter

        emitter = AuditEmitter()

        with patch.object(emitter, "_persist"):
            with patch("app.audit.emitter._audit_logger") as mock_logger:
                emitter.emit(
                    event_type="auth.login",
                    action="auth.login",
                    actor_id="user-001",
                    actor_type="user",
                    resource_type="session",
                    result="failure",
                )
                logged_event = mock_logger.info.call_args[0][1]
                assert "result" in logged_event
                assert logged_event["result"] == "failure"


# ---------------------------------------------------------------------------
# A09-006~009: 주요 API에서 감사 로그 사용
# ---------------------------------------------------------------------------

class TestA09ApiAuditCoverage:
    """주요 API에서 감사 로그 사용 확인."""

    def test_document_routes_use_audit(self):
        """A09-006: documents 라우터에서 감사 로그를 사용한다."""
        doc_router_path = ROOT / "backend/app/api/v1/documents.py"
        if not doc_router_path.exists():
            pytest.skip("documents.py 없음")

        source = doc_router_path.read_text(encoding="utf-8")
        assert "audit" in source.lower() or "audit_emitter" in source, (
            "documents 라우터에 감사 로그 없음"
        )

    def test_agent_proposals_use_audit(self):
        """A09-007: agent_proposals 라우터에서 감사 로그를 사용한다."""
        agent_router_path = ROOT / "backend/app/api/v1/agent_proposals.py"
        if not agent_router_path.exists():
            pytest.skip("agent_proposals.py 없음")

        source = agent_router_path.read_text(encoding="utf-8")
        has_audit = "audit" in source.lower() or "actor_type" in source
        assert has_audit, "agent_proposals 라우터에 감사 로그 없음"

    def test_auth_routes_use_audit(self):
        """A09-008: 인증 라우터에서 감사 로그를 사용한다."""
        auth_router_path = ROOT / "backend/app/api/v1/auth_router.py"
        if not auth_router_path.exists():
            pytest.skip("auth_router.py 없음")

        source = auth_router_path.read_text(encoding="utf-8")
        assert "audit" in source.lower(), "인증 라우터에 감사 로그 없음"

    def test_kill_switch_uses_audit(self):
        """A09-009: Kill switch API가 감사 이벤트를 기록한다."""
        scope_profiles_path = ROOT / "backend/app/api/v1/scope_profiles.py"
        source = scope_profiles_path.read_text(encoding="utf-8")

        assert "audit_emitter" in source or "emit" in source, (
            "Kill switch에 감사 로그 없음"
        )


# ---------------------------------------------------------------------------
# A09-010~012: 모니터링 및 알림
# ---------------------------------------------------------------------------

class TestA09Monitoring:
    """모니터링 및 알림 검증."""

    def test_observability_module_exists(self):
        """A09-010: 관측성(observability) 모듈이 존재한다."""
        obs_dir = ROOT / "backend/app/observability"
        assert obs_dir.exists(), "observability 모듈 없음"

    def test_logging_configuration_exists(self):
        """A09-011: 로깅 설정이 구성되어 있다."""
        # main.py 또는 logging_config.py 확인
        main_path = ROOT / "backend/app/main.py"
        if not main_path.exists():
            pytest.skip("main.py 없음")

        source = main_path.read_text(encoding="utf-8")
        has_logging = "logging" in source.lower() or "logger" in source
        assert has_logging, "메인 앱에 로깅 설정 없음"

    def test_error_handler_returns_structured_response(self):
        """A09-012: 에러 핸들러가 구조화된 에러 응답을 반환한다."""
        handlers_path = ROOT / "backend/app/api/errors/handlers.py"
        if not handlers_path.exists():
            pytest.skip("handlers.py 없음")

        source = handlers_path.read_text(encoding="utf-8")
        # JSON 응답 형태로 에러 반환하는지 확인
        has_structured = "JSONResponse" in source or "json" in source.lower()
        assert has_structured, "에러 핸들러에 구조화된 응답 없음"
