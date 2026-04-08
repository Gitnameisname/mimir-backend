"""
Role / UserOrgRole 도메인 모델 (순수 Python dataclass).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Role:
    """역할 도메인 모델.

    Attributes:
        id          : UUID
        name        : 역할명 (unique) — VIEWER / AUTHOR / REVIEWER / APPROVER / ORG_ADMIN / SUPER_ADMIN
        description : 설명
        is_system   : 시스템 기본 역할 여부 (True이면 삭제 불가)
        created_at  : 생성 시각
    """

    id: str
    name: str
    is_system: bool
    created_at: datetime
    description: Optional[str] = None


@dataclass
class UserOrgRole:
    """사용자-조직-역할 매핑 도메인 모델.

    Attributes:
        id         : UUID
        user_id    : 사용자 UUID
        org_id     : 조직 UUID
        role_name  : 해당 조직에서의 역할명
        created_at : 생성 시각
    """

    id: str
    user_id: str
    org_id: str
    role_name: str
    created_at: datetime
