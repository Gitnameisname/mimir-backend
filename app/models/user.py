"""
User 도메인 모델 (순수 Python dataclass).

ORM에 의존하지 않으며, repository가 DB row → User로 변환해 반환한다.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class User:
    """사용자 도메인 모델.

    Attributes:
        id                : UUID
        email             : 이메일 (unique)
        username          : 아이디 (unique, nullable, 대소문자 무시)
        display_name      : 표시 이름
        status            : ACTIVE / INACTIVE / SUSPENDED
        role_name         : 전역 역할 (VIEWER / AUTHOR / REVIEWER / APPROVER / ORG_ADMIN / SUPER_ADMIN)
        last_login_at     : 최근 로그인 시각
        created_at        : 생성 시각
        updated_at        : 최종 수정 시각
        password_hash     : bcrypt 해싱된 비밀번호 (Phase 14)
        auth_provider     : 인증 공급자 (local / gitlab) (Phase 14)
        email_verified    : 이메일 인증 여부 (Phase 14)
        email_verified_at : 이메일 인증 시각 (Phase 14)
        failed_login_count: 연속 로그인 실패 횟수 (Phase 14)
        locked_until      : 계정 잠금 해제 시각 (Phase 14)
        avatar_url        : 프로필 이미지 URL (Phase 14)
    """

    id: str
    email: str
    display_name: str
    status: str
    role_name: str
    created_at: datetime
    updated_at: datetime
    username: Optional[str] = None
    last_login_at: Optional[datetime] = None
    # Phase 14 인증 확장 필드
    password_hash: Optional[str] = None
    auth_provider: str = "local"
    email_verified: bool = False
    email_verified_at: Optional[datetime] = None
    failed_login_count: int = 0
    locked_until: Optional[datetime] = None
    avatar_url: Optional[str] = None
