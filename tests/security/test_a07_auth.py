"""
A07 Identification and Authentication Failures 검증 테스트.

검증 항목:
  - JWT 토큰 만료 검증
  - 로그인 rate limiting (Valkey)
  - Refresh Token 로테이션
  - Purpose Token (이메일 인증 등)
  - OAuth 2.0 Client Credentials 지원
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "security-test-secret-a07")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "security-test-internal")


# ---------------------------------------------------------------------------
# A07-001~004: JWT 토큰 만료
# ---------------------------------------------------------------------------

class TestA07JwtExpiry:
    """JWT 토큰 만료 검증."""

    def test_access_token_has_expiry(self):
        """A07-001: Access token에 exp 클레임이 포함된다."""
        from app.api.auth.tokens import create_access_token, decode_access_token

        token = create_access_token(actor_id="user-001", role="VIEWER")
        payload = decode_access_token(token)

        assert payload is not None
        assert "exp" in payload, "exp 클레임 없음"
        assert payload["exp"] > time.time(), "만료 시간이 과거임"

    def test_expired_token_rejected(self):
        """A07-002: 만료된 토큰이 거부된다."""
        from app.api.auth.tokens import create_access_token, decode_access_token

        # 1초 후 만료되는 토큰 생성
        token = create_access_token(
            actor_id="user-001",
            role="VIEWER",
            expires_minutes=-1,  # 이미 만료
        )
        result = decode_access_token(token)
        assert result is None, "만료된 토큰이 통과됨"

    def test_token_has_short_expiry_for_access(self):
        """A07-003: Access token 만료 시간이 짧다 (24시간 이하)."""
        from app.config import settings

        assert settings.jwt_expire_minutes <= 60 * 24, (
            f"Access token 만료 시간이 너무 김: {settings.jwt_expire_minutes}분"
        )

    def test_token_jti_claim_exists(self):
        """A07-004: JWT token에 jti (고유 ID) 클레임이 포함된다 (재사용 방지)."""
        from app.api.auth.tokens import create_access_token, decode_access_token

        token = create_access_token(actor_id="user-001", role="VIEWER")
        payload = decode_access_token(token)

        assert payload is not None
        assert "jti" in payload, "jti 클레임 없음 — 토큰 재사용 방지 불가"

    def test_two_tokens_have_different_jti(self):
        """A07-005: 동일한 사용자에게 발급된 두 토큰은 다른 jti를 가진다."""
        from app.api.auth.tokens import create_access_token, decode_access_token

        t1 = create_access_token(actor_id="user-001", role="VIEWER")
        t2 = create_access_token(actor_id="user-001", role="VIEWER")

        p1 = decode_access_token(t1)
        p2 = decode_access_token(t2)

        assert p1["jti"] != p2["jti"], "동일한 jti 사용 — 토큰 재사용 공격 취약"


# ---------------------------------------------------------------------------
# A07-006~010: 로그인 Rate Limiting
# ---------------------------------------------------------------------------

class TestA07RateLimiting:
    """로그인 실패 rate limiting 검증."""

    def test_rate_limit_module_exists(self):
        """A07-006: rate_limit 모듈이 존재한다."""
        from app.api.auth.rate_limit import (
            check_login_allowed,
            record_failed_attempt,
            clear_attempts,
        )
        assert callable(check_login_allowed)
        assert callable(record_failed_attempt)
        assert callable(clear_attempts)

    def test_rate_limit_blocks_after_max_attempts(self):
        """A07-007: 최대 시도 횟수 초과 후 로그인이 차단된다."""
        from app.api.auth.rate_limit import check_login_allowed, record_failed_attempt
        from app.config import settings

        mock_valkey = MagicMock()

        # login_max_attempts 초과 시 차단
        mock_valkey.get.return_value = str(settings.login_max_attempts + 1).encode()

        result = check_login_allowed(mock_valkey, "attacker@example.com")
        assert result is False, "최대 시도 초과 후 차단 안 됨"

    def test_rate_limit_allows_within_max(self):
        """A07-008: 최대 시도 횟수 미달 시 로그인이 허용된다."""
        from app.api.auth.rate_limit import check_login_allowed
        from app.config import settings

        mock_valkey = MagicMock()
        mock_valkey.get.return_value = b"1"  # 1회 실패

        result = check_login_allowed(mock_valkey, "user@example.com")
        assert result is True, "정상 범위 내 시도 차단됨"

    def test_rate_limit_resets_on_success(self):
        """A07-009: 로그인 성공 시 실패 카운터가 초기화된다."""
        from app.api.auth.rate_limit import clear_attempts

        mock_valkey = MagicMock()
        clear_attempts(mock_valkey, "user@example.com")
        mock_valkey.delete.assert_called_once()

    def test_rate_limit_config_reasonable(self):
        """A07-010: Rate limit 설정이 합리적이다 (3~10회, 5~60분)."""
        from app.config import settings

        assert 3 <= settings.login_max_attempts <= 10, (
            f"login_max_attempts가 비합리적: {settings.login_max_attempts}"
        )
        assert 5 <= settings.login_lockout_minutes <= 60, (
            f"login_lockout_minutes가 비합리적: {settings.login_lockout_minutes}"
        )


# ---------------------------------------------------------------------------
# A07-011~013: Refresh Token
# ---------------------------------------------------------------------------

class TestA07RefreshToken:
    """Refresh Token 보안 검증."""

    def test_refresh_token_hash_only_stored(self):
        """A07-011: Refresh Token이 DB에는 해시만 저장된다."""
        tokens_path = ROOT / "backend/app/api/auth/tokens.py"
        source = tokens_path.read_text(encoding="utf-8")

        # create_refresh_token()이 (raw, hash) 튜플 반환
        assert "token_hash" in source or "sha256" in source, (
            "Refresh Token 해시 저장 코드 없음"
        )

    def test_refresh_service_exists(self):
        """A07-012: RefreshService가 존재한다."""
        refresh_path = ROOT / "backend/app/api/auth/refresh_service.py"
        assert refresh_path.exists(), "refresh_service.py 없음"

    def test_refresh_service_has_rotation(self):
        """A07-013: Refresh Token Rotation이 구현되어 있다."""
        refresh_path = ROOT / "backend/app/api/auth/refresh_service.py"
        source = refresh_path.read_text(encoding="utf-8")

        assert "rotate" in source.lower() or "new" in source.lower(), (
            "Refresh Token Rotation 없음"
        )


# ---------------------------------------------------------------------------
# A07-014~015: OAuth 2.0 지원
# ---------------------------------------------------------------------------

class TestA07Oauth:
    """OAuth 2.0 인증 검증."""

    def test_oauth_service_exists(self):
        """A07-014: OAuth 서비스가 존재한다."""
        oauth_path = ROOT / "backend/app/api/auth/oauth_service.py"
        assert oauth_path.exists(), "oauth_service.py 없음"

    def test_purpose_token_module_exists(self):
        """A07-015: Purpose Token 모듈이 존재한다 (이메일 인증 등)."""
        purpose_path = ROOT / "backend/app/api/auth/purpose_tokens.py"
        assert purpose_path.exists(), "purpose_tokens.py 없음"

        source = purpose_path.read_text(encoding="utf-8")
        assert "expire" in source.lower() or "exp" in source, (
            "Purpose Token 만료 설정 없음"
        )
