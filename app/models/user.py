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
        id           : UUID
        email        : 이메일 (unique)
        display_name : 표시 이름
        status       : ACTIVE / INACTIVE / SUSPENDED
        role_name    : 전역 역할 (VIEWER / AUTHOR / REVIEWER / APPROVER / ORG_ADMIN / SUPER_ADMIN)
        last_login_at: 최근 로그인 시각
        created_at   : 생성 시각
        updated_at   : 최종 수정 시각
    """

    id: str
    email: str
    display_name: str
    status: str
    role_name: str
    created_at: datetime
    updated_at: datetime
    last_login_at: Optional[datetime] = None
