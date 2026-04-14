"""
비밀번호 해싱 유틸리티 (Phase 14).

bcrypt 라이브러리를 직접 사용하여 해싱/검증을 제공한다.

보안 원칙:
  - 평문 비밀번호는 로그에 절대 기록하지 않는다.
  - bcrypt 최대 입력 길이(72바이트)를 초과하는 비밀번호는 자동 truncation됨.
    → 128자 제한을 두어 사실상 72바이트 이내로 유지.
"""

import logging

import bcrypt

from app.config import settings

logger = logging.getLogger(__name__)

_COST_FACTOR = settings.bcrypt_cost_factor

# 타이밍 공격 방어용 더미 해시 (사용자 미존재 시 동일 응답 시간 유지)
_DUMMY_HASH = bcrypt.hashpw(
    b"dummy-password-for-timing-safety",
    bcrypt.gensalt(rounds=_COST_FACTOR),
)


def hash_password(plain: str) -> str:
    """평문 비밀번호를 bcrypt로 해싱한다.

    Args:
        plain: 평문 비밀번호 (최대 128자)

    Returns:
        bcrypt 해시 문자열 (예: $2b$12$...)
    """
    salt = bcrypt.gensalt(rounds=_COST_FACTOR)
    hashed = bcrypt.hashpw(plain.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """평문 비밀번호와 해시를 비교한다.

    Args:
        plain: 입력된 평문 비밀번호
        hashed: DB에 저장된 bcrypt 해시

    Returns:
        일치 여부
    """
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def dummy_verify() -> None:
    """사용자 미존재 시 호출하여 타이밍 공격을 방어한다.

    verify_password와 동일한 CPU 시간을 소모하므로,
    응답 시간으로 사용자 존재 여부를 추론할 수 없다.
    """
    bcrypt.checkpw(b"dummy-input", _DUMMY_HASH)
