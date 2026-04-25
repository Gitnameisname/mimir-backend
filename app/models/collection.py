"""
Collection 도메인 모델 — S3 Phase 2 FG 2-1.

사용자가 만든 임의 문서 집합(컬렉션). 순수 뷰 레이어이며 ACL 에 영향을 주지 않는다.
N:M 관계이므로 하나의 문서가 여러 컬렉션에 속할 수 있다.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Collection:
    """컬렉션 도메인 모델.

    Attributes:
        id          : UUID (str)
        owner_id    : 컬렉션 소유자 (users.id)
        name        : 컬렉션 이름 (owner 범위 내 UNIQUE)
        description : 선택 설명
        created_at  : 생성 시각
        updated_at  : 최종 수정 시각
        document_count : repository 가 선택적으로 계산해서 채우는 필드. None 이면 미계산.
    """

    id: str
    owner_id: str
    name: str
    description: Optional[str]
    created_at: datetime
    updated_at: datetime
    document_count: Optional[int] = None


@dataclass
class CollectionDocument:
    """컬렉션-문서 연결 레코드.

    Attributes:
        collection_id : 컬렉션 UUID
        document_id   : 문서 UUID
        position      : 정렬 힌트 (드래그-드롭 재배치는 Phase 2 후속/Phase 3)
        added_at      : 추가 시각
    """

    collection_id: str
    document_id: str
    position: int
    added_at: datetime
