"""
Organization 도메인 모델 (순수 Python dataclass).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Organization:
    """조직 도메인 모델.

    Attributes:
        id          : UUID
        name        : 조직명
        description : 설명
        status      : ACTIVE / INACTIVE
        created_at  : 생성 시각
        updated_at  : 최종 수정 시각
    """

    id: str
    name: str
    status: str
    created_at: datetime
    updated_at: datetime
    description: Optional[str] = None
