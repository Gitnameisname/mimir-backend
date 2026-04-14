"""
Phase 14-4 GitLab OAuth 단위 테스트.

테스트 대상:
  - PKCE (code_verifier / code_challenge) 생성 및 검증
  - OAuth state 생성
  - AES-256-GCM 토큰 암호화/복호화
  - 계정 연결/생성 로직 (DB mock)
"""

import base64
import hashlib
import json
import os
import secrets

import pytest


class TestPKCE:
    """PKCE code_verifier / code_challenge 테스트."""

    def test_code_verifier_length(self):
        """code_verifier가 충분한 길이를 가진다."""
        from app.api.auth.oauth_service import _generate_code_verifier

        verifier = _generate_code_verifier()
        # base64url of 64 bytes → ~86 chars
        assert len(verifier) >= 43  # RFC 7636 최소 길이
        assert len(verifier) <= 128  # RFC 7636 최대 길이

    def test_code_verifier_unique(self):
        """매번 다른 code_verifier가 생성된다."""
        from app.api.auth.oauth_service import _generate_code_verifier

        v1 = _generate_code_verifier()
        v2 = _generate_code_verifier()
        assert v1 != v2

    def test_code_challenge_s256(self):
        """S256 code_challenge가 올바르게 계산된다."""
        from app.api.auth.oauth_service import _generate_code_challenge

        # RFC 7636 Appendix B 기반 수동 검증
        verifier = "test-verifier-string"
        challenge = _generate_code_challenge(verifier)

        # 수동 계산
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        assert challenge == expected

    def test_code_challenge_no_padding(self):
        """S256 code_challenge에 base64 패딩(=)이 없다."""
        from app.api.auth.oauth_service import _generate_code_challenge, _generate_code_verifier

        verifier = _generate_code_verifier()
        challenge = _generate_code_challenge(verifier)
        assert "=" not in challenge


class TestOAuthState:
    """OAuth state 파라미터 테스트."""

    def test_state_length(self):
        """state가 충분한 길이를 가진다."""
        from app.api.auth.oauth_service import _generate_state

        state = _generate_state()
        assert len(state) >= 20  # 보안상 충분한 엔트로피

    def test_state_unique(self):
        """매번 다른 state가 생성된다."""
        from app.api.auth.oauth_service import _generate_state

        s1 = _generate_state()
        s2 = _generate_state()
        assert s1 != s2


class TestEncryption:
    """AES-256-GCM 토큰 암호화/복호화 테스트."""

    def test_encrypt_decrypt_roundtrip(self):
        """암호화 → 복호화 라운드트립이 성공한다."""
        from app.api.auth.encryption import encrypt_token, decrypt_token, _get_encryption_key
        from app.config import settings

        # 테스트용 키 설정
        test_key = os.urandom(32)
        original_key = settings.oauth_token_encryption_key
        settings.oauth_token_encryption_key = base64.b64encode(test_key).decode()

        try:
            plaintext = "gho_test_token_12345"
            encrypted = encrypt_token(plaintext)
            assert encrypted is not None
            assert encrypted != plaintext  # 암호화됨

            decrypted = decrypt_token(encrypted)
            assert decrypted == plaintext  # 원본 복원
        finally:
            settings.oauth_token_encryption_key = original_key

    def test_different_ciphertexts(self):
        """같은 평문이라도 매번 다른 암호문이 생성된다 (nonce 랜덤)."""
        from app.api.auth.encryption import encrypt_token
        from app.config import settings

        test_key = os.urandom(32)
        original_key = settings.oauth_token_encryption_key
        settings.oauth_token_encryption_key = base64.b64encode(test_key).decode()

        try:
            plaintext = "same-token"
            enc1 = encrypt_token(plaintext)
            enc2 = encrypt_token(plaintext)
            assert enc1 != enc2  # nonce가 다르므로 암호문도 다름
        finally:
            settings.oauth_token_encryption_key = original_key

    def test_tampered_ciphertext_fails(self):
        """변조된 암호문은 복호화에 실패한다 (GCM 인증)."""
        from app.api.auth.encryption import encrypt_token, decrypt_token
        from app.config import settings

        test_key = os.urandom(32)
        original_key = settings.oauth_token_encryption_key
        settings.oauth_token_encryption_key = base64.b64encode(test_key).decode()

        try:
            plaintext = "secret-token"
            encrypted = encrypt_token(plaintext)

            # 암호문 변조
            data = base64.b64decode(encrypted)
            tampered = data[:-1] + bytes([(data[-1] + 1) % 256])
            tampered_b64 = base64.b64encode(tampered).decode()

            result = decrypt_token(tampered_b64)
            assert result is None  # GCM 인증 실패
        finally:
            settings.oauth_token_encryption_key = original_key

    def test_no_key_returns_plaintext(self):
        """암호화 키 미설정 시 평문 그대로 반환한다."""
        from app.api.auth.encryption import encrypt_token, decrypt_token
        from app.config import settings

        original_key = settings.oauth_token_encryption_key
        settings.oauth_token_encryption_key = ""

        try:
            plaintext = "plain-token"
            result = encrypt_token(plaintext)
            assert result == plaintext  # 키 없으면 평문

            decrypted = decrypt_token(plaintext)
            assert decrypted == plaintext
        finally:
            settings.oauth_token_encryption_key = original_key


class TestOIDCDiscovery:
    """OIDC Discovery URL 구성 테스트."""

    def test_discovery_url_gitlab_com(self):
        """GitLab.com Discovery URL이 올바르다."""
        from app.api.auth.oauth_service import _get_discovery_url
        from app.config import settings

        original = settings.gitlab_base_url
        settings.gitlab_base_url = "https://gitlab.com"

        try:
            assert _get_discovery_url() == "https://gitlab.com/.well-known/openid-configuration"
        finally:
            settings.gitlab_base_url = original

    def test_discovery_url_self_managed(self):
        """Self-managed GitLab Discovery URL이 올바르다."""
        from app.api.auth.oauth_service import _get_discovery_url
        from app.config import settings

        original = settings.gitlab_base_url
        settings.gitlab_base_url = "https://gitlab.mycompany.com/"

        try:
            # trailing slash 제거됨
            assert _get_discovery_url() == "https://gitlab.mycompany.com/.well-known/openid-configuration"
        finally:
            settings.gitlab_base_url = original


class TestConfigOAuthEnabled:
    """config.py is_oauth_enabled 속성 테스트."""

    def test_oauth_disabled_by_default(self):
        """기본 설정에서 OAuth가 비활성이다."""
        from app.config import settings

        original_id = settings.gitlab_client_id
        original_secret = settings.gitlab_client_secret
        settings.gitlab_client_id = ""
        settings.gitlab_client_secret = ""

        try:
            assert settings.is_oauth_enabled is False
        finally:
            settings.gitlab_client_id = original_id
            settings.gitlab_client_secret = original_secret

    def test_oauth_enabled_when_configured(self):
        """Client ID + Secret이 설정되면 OAuth가 활성이다."""
        from app.config import settings

        original_id = settings.gitlab_client_id
        original_secret = settings.gitlab_client_secret
        settings.gitlab_client_id = "test-client-id"
        settings.gitlab_client_secret = "test-client-secret"

        try:
            assert settings.is_oauth_enabled is True
        finally:
            settings.gitlab_client_id = original_id
            settings.gitlab_client_secret = original_secret
