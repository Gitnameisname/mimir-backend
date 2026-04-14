"""
Phase 14 인증 모듈 단위 테스트.

테스트 대상:
  - password.py: bcrypt 해싱/검증, 더미 검증
  - validators.py: 비밀번호 복잡도 검증
  - rate_limit.py: 로그인 시도 제한 (fakeredis 사용)
"""

import pytest


# ---------------------------------------------------------------------------
# 1. password.py 테스트
# ---------------------------------------------------------------------------

class TestPasswordHashing:
    """bcrypt 해싱 및 검증 테스트."""

    def test_hash_password_returns_bcrypt_hash(self):
        """hash_password()가 $2b$ 접두사의 bcrypt 해시를 반환한다."""
        from app.api.auth.password import hash_password

        hashed = hash_password("TestP@ss1")
        assert hashed.startswith("$2b$")
        assert len(hashed) == 60

    def test_verify_password_correct(self):
        """올바른 비밀번호가 True를 반환한다."""
        from app.api.auth.password import hash_password, verify_password

        plain = "SecureP@ss123"
        hashed = hash_password(plain)
        assert verify_password(plain, hashed) is True

    def test_verify_password_wrong(self):
        """잘못된 비밀번호가 False를 반환한다."""
        from app.api.auth.password import hash_password, verify_password

        hashed = hash_password("CorrectP@ss1")
        assert verify_password("WrongP@ss1", hashed) is False

    def test_hash_is_different_each_time(self):
        """동일 비밀번호로 해싱해도 매번 다른 해시가 생성된다 (salt)."""
        from app.api.auth.password import hash_password

        h1 = hash_password("SamePassword1!")
        h2 = hash_password("SamePassword1!")
        assert h1 != h2

    def test_dummy_verify_does_not_raise(self):
        """dummy_verify()가 예외 없이 실행된다."""
        from app.api.auth.password import dummy_verify

        # 단순히 예외 없이 수행되는지만 확인
        dummy_verify()


# ---------------------------------------------------------------------------
# 2. validators.py 테스트
# ---------------------------------------------------------------------------

class TestPasswordValidation:
    """비밀번호 복잡도 검증 테스트."""

    def test_valid_password(self):
        """유효한 비밀번호는 빈 에러 리스트를 반환한다."""
        from app.api.auth.validators import validate_password_strength

        assert validate_password_strength("SecureP@1") == []
        assert validate_password_strength("abc12345") == []
        assert validate_password_strength("!!aabbcc") == []

    def test_too_short(self):
        """8자 미만 비밀번호는 에러를 반환한다."""
        from app.api.auth.validators import validate_password_strength

        errors = validate_password_strength("Ab1!")
        assert any("8자" in e for e in errors)

    def test_too_long(self):
        """128자 초과 비밀번호는 에러를 반환한다."""
        from app.api.auth.validators import validate_password_strength

        errors = validate_password_strength("a" * 129)
        assert any("128자" in e for e in errors)

    def test_single_category(self):
        """한 종류 문자만 사용하면 에러를 반환한다."""
        from app.api.auth.validators import validate_password_strength

        # 영문만
        errors = validate_password_strength("abcdefghij")
        assert any("2종류" in e for e in errors)

        # 숫자만
        errors = validate_password_strength("1234567890")
        assert any("2종류" in e for e in errors)

    def test_two_categories_pass(self):
        """2종류 이상 문자를 사용하면 통과한다."""
        from app.api.auth.validators import validate_password_strength

        # 영문 + 숫자
        assert validate_password_strength("abcdef12") == []
        # 영문 + 특수문자
        assert validate_password_strength("abcdef!@") == []
        # 숫자 + 특수문자
        assert validate_password_strength("123456!@") == []

    def test_empty_password(self):
        """빈 비밀번호는 길이와 복잡도 모두 실패한다."""
        from app.api.auth.validators import validate_password_strength

        errors = validate_password_strength("")
        assert len(errors) >= 2  # 길이 + 복잡도


class TestDisplayNameValidation:
    """표시 이름 검증 테스트."""

    def test_valid_name(self):
        from app.api.auth.validators import validate_display_name

        assert validate_display_name("홍길동") == []
        assert validate_display_name("John Doe") == []

    def test_empty_name(self):
        from app.api.auth.validators import validate_display_name

        errors = validate_display_name("")
        assert len(errors) > 0

    def test_whitespace_only(self):
        from app.api.auth.validators import validate_display_name

        errors = validate_display_name("   ")
        assert len(errors) > 0

    def test_too_long(self):
        from app.api.auth.validators import validate_display_name

        errors = validate_display_name("a" * 101)
        assert any("100자" in e for e in errors)


# ---------------------------------------------------------------------------
# 3. rate_limit.py 테스트 (fakeredis)
# ---------------------------------------------------------------------------

class TestRateLimit:
    """로그인 시도 제한 테스트 (fakeredis 사용)."""

    def _get_fake_valkey(self):
        """fakeredis 클라이언트를 생성한다. 설치되어 있지 않으면 스킵."""
        try:
            import fakeredis
            return fakeredis.FakeRedis(decode_responses=True)
        except ImportError:
            pytest.skip("fakeredis not installed")

    def test_initial_login_allowed(self):
        """초기 상태에서 로그인이 허용된다."""
        from app.api.auth.rate_limit import check_login_allowed

        fake_valkey = self._get_fake_valkey()
        assert check_login_allowed(fake_valkey, "test@example.com") is True

    def test_record_and_check_attempts(self):
        """실패 기록 후 횟수가 증가한다."""
        from app.api.auth.rate_limit import check_login_allowed, record_failed_attempt

        fake_valkey = self._get_fake_valkey()
        email = "test@example.com"

        for i in range(1, 5):
            count = record_failed_attempt(fake_valkey, email)
            assert count == i
            assert check_login_allowed(fake_valkey, email) is True

    def test_lockout_after_max_attempts(self):
        """5회 실패 후 잠금된다."""
        from app.api.auth.rate_limit import check_login_allowed, record_failed_attempt

        fake_valkey = self._get_fake_valkey()
        email = "locked@example.com"

        for _ in range(5):
            record_failed_attempt(fake_valkey, email)

        assert check_login_allowed(fake_valkey, email) is False

    def test_clear_attempts(self):
        """성공 시 카운터가 초기화된다."""
        from app.api.auth.rate_limit import check_login_allowed, clear_attempts, record_failed_attempt

        fake_valkey = self._get_fake_valkey()
        email = "clear@example.com"

        for _ in range(3):
            record_failed_attempt(fake_valkey, email)

        clear_attempts(fake_valkey, email)
        assert check_login_allowed(fake_valkey, email) is True

    def test_different_emails_independent(self):
        """서로 다른 이메일의 카운터는 독립적이다."""
        from app.api.auth.rate_limit import check_login_allowed, record_failed_attempt

        fake_valkey = self._get_fake_valkey()

        for _ in range(5):
            record_failed_attempt(fake_valkey, "locked@example.com")

        assert check_login_allowed(fake_valkey, "locked@example.com") is False
        assert check_login_allowed(fake_valkey, "other@example.com") is True
