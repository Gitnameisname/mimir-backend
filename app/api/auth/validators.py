"""
입력값 검증 유틸리티 (Phase 14).

비밀번호 복잡도, 이메일 형식 등 인증 관련 입력값을 검증한다.

정책:
  - 비밀번호: 8자 이상, 128자 이하, 2종류 이상 문자 유형
  - 이메일: 기본 형식 검증 (Pydantic EmailStr 보완)
"""

import re


# 비밀번호 최대 길이 (bcrypt 72바이트 제한 + 여유)
_PASSWORD_MAX_LENGTH = 128
_PASSWORD_MIN_LENGTH = 8


def validate_password_strength(password: str) -> list[str]:
    """비밀번호 복잡도를 검증한다.

    규칙:
      1. 최소 8자 이상
      2. 최대 128자 이하
      3. 영문, 숫자, 특수문자 중 2종류 이상 포함

    Args:
        password: 검증할 비밀번호 평문

    Returns:
        에러 메시지 리스트. 빈 리스트이면 통과.
    """
    errors: list[str] = []

    if len(password) < _PASSWORD_MIN_LENGTH:
        errors.append(f"비밀번호는 최소 {_PASSWORD_MIN_LENGTH}자 이상이어야 합니다")

    if len(password) > _PASSWORD_MAX_LENGTH:
        errors.append(f"비밀번호는 최대 {_PASSWORD_MAX_LENGTH}자 이하여야 합니다")

    # 문자 유형 카운트
    categories = 0
    if re.search(r"[a-zA-Z]", password):
        categories += 1
    if re.search(r"[0-9]", password):
        categories += 1
    if re.search(r"""[!@#$%^&*()\-_=+\[\]{}|;:'",.<>?/`~\\]""", password):
        categories += 1

    if categories < 2:
        errors.append("영문, 숫자, 특수문자 중 2종류 이상 포함해야 합니다")

    return errors


# 아이디 규칙
_USERNAME_MIN_LENGTH = 3
_USERNAME_MAX_LENGTH = 30
_USERNAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
# 예약어/금지어 (라우팅 충돌 및 혼동 방지)
_USERNAME_RESERVED = frozenset(
    {"admin", "root", "system", "anonymous", "null", "undefined", "me", "self"}
)


def validate_username(username: str) -> list[str]:
    """아이디(username)를 검증한다.

    규칙:
      1. 3~30자
      2. 영문자로 시작해야 함
      3. 영문자, 숫자, '_', '.', '-' 만 허용
      4. 예약어 금지 (admin, root 등)
      5. 이메일 형식 금지 ('@' 포함 불가 — 이메일과 충돌)

    Args:
        username: 검증할 아이디

    Returns:
        에러 메시지 리스트. 빈 리스트이면 통과.
    """
    errors: list[str] = []
    value = username.strip()

    if not value:
        errors.append("아이디를 입력해 주세요")
        return errors

    if len(value) < _USERNAME_MIN_LENGTH:
        errors.append(f"아이디는 최소 {_USERNAME_MIN_LENGTH}자 이상이어야 합니다")
    if len(value) > _USERNAME_MAX_LENGTH:
        errors.append(f"아이디는 최대 {_USERNAME_MAX_LENGTH}자 이하여야 합니다")
    if "@" in value:
        errors.append("아이디에는 '@' 문자를 포함할 수 없습니다")
    if not _USERNAME_PATTERN.match(value):
        errors.append(
            "아이디는 영문자로 시작하고 영문/숫자/._- 만 사용할 수 있습니다"
        )
    if value.lower() in _USERNAME_RESERVED:
        errors.append("사용할 수 없는 아이디입니다")

    return errors


def validate_display_name(name: str) -> list[str]:
    """표시 이름을 검증한다.

    Args:
        name: 검증할 표시 이름

    Returns:
        에러 메시지 리스트. 빈 리스트이면 통과.
    """
    errors: list[str] = []

    stripped = name.strip()
    if not stripped:
        errors.append("표시 이름은 비어있을 수 없습니다")
    elif len(stripped) > 100:
        errors.append("표시 이름은 최대 100자 이하여야 합니다")

    return errors
