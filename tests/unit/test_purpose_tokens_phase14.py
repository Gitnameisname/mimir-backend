"""
Phase 14-5 목적별 JWT 토큰 및 이메일 서비스 단위 테스트.

테스트 대상:
  - create_purpose_token: 목적별 토큰 생성
  - decode_purpose_token: 디코딩 + 목적 검증
  - check_token_used / mark_token_used: 1회성 보장 (Valkey)
  - EmailService: 이메일 발송 템플릿
"""

import pytest


class TestPurposeTokens:
    """목적별 JWT 토큰 테스트."""

    def test_create_and_decode_password_reset(self):
        """password_reset 토큰 생성/디코딩이 성공한다."""
        from app.api.auth.purpose_tokens import create_purpose_token, decode_purpose_token

        token = create_purpose_token("user-123", purpose="password_reset", expire_minutes=30)
        assert token  # JWT 문자열

        payload = decode_purpose_token(token, expected_purpose="password_reset")
        assert payload is not None
        assert payload["sub"] == "user-123"
        assert payload["purpose"] == "password_reset"
        assert "jti" in payload
        assert "exp" in payload

    def test_create_and_decode_email_verify(self):
        """email_verify 토큰 생성/디코딩이 성공한다."""
        from app.api.auth.purpose_tokens import create_purpose_token, decode_purpose_token

        token = create_purpose_token("user-456", purpose="email_verify", expire_minutes=1440)
        payload = decode_purpose_token(token, expected_purpose="email_verify")
        assert payload is not None
        assert payload["sub"] == "user-456"
        assert payload["purpose"] == "email_verify"

    def test_purpose_mismatch_rejected(self):
        """다른 목적으로 디코딩 시 실패한다."""
        from app.api.auth.purpose_tokens import create_purpose_token, decode_purpose_token

        token = create_purpose_token("user-123", purpose="password_reset")
        payload = decode_purpose_token(token, expected_purpose="email_verify")
        assert payload is None

    def test_invalid_token_returns_none(self):
        """유효하지 않은 토큰은 None을 반환한다."""
        from app.api.auth.purpose_tokens import decode_purpose_token

        assert decode_purpose_token("invalid.jwt.token", "password_reset") is None

    def test_jti_is_unique(self):
        """매번 다른 jti가 생성된다."""
        from app.api.auth.purpose_tokens import create_purpose_token, decode_purpose_token

        t1 = create_purpose_token("user-123", purpose="password_reset")
        t2 = create_purpose_token("user-123", purpose="password_reset")

        p1 = decode_purpose_token(t1, "password_reset")
        p2 = decode_purpose_token(t2, "password_reset")
        assert p1["jti"] != p2["jti"]


class TestEmailServiceTemplates:
    """이메일 서비스 템플릿 테스트."""

    def test_is_configured_false_when_empty(self):
        """SMTP 미설정 시 is_configured가 False."""
        from app.services.email_service import EmailService
        from app.config import settings

        original = settings.smtp_host
        settings.smtp_host = ""
        try:
            svc = EmailService()
            assert svc.is_configured is False
        finally:
            settings.smtp_host = original

    def test_send_email_returns_false_when_not_configured(self):
        """SMTP 미설정 시 send_email이 False를 반환한다."""
        from app.services.email_service import EmailService
        from app.config import settings

        original = settings.smtp_host
        settings.smtp_host = ""
        try:
            svc = EmailService()
            result = svc.send_email("test@example.com", "subject", "body")
            assert result is False
        finally:
            settings.smtp_host = original
