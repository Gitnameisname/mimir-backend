"""
Node 도메인 모델 (순수 Python dataclass).

문서 버전의 구조 단위를 표현한다.
flat list + parent_id + order_index 방식으로 트리를 표현한다.

AI/RAG citation-friendly:
  - stable id (UUID)
  - version_id → 어느 버전의 노드인지 참조
  - parent_id + order_index → 구조 탐색 가능
  - node_type → paragraph / heading / section 등으로 의미 구분
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


@dataclass
class Node:
    """문서 노드 도메인 모델.

    Attributes:
        id          : UUID (citation-friendly stable identifier)
        version_id  : 소속 버전 UUID
        parent_id   : 부모 노드 UUID (None = 최상위)
        node_type   : 노드 타입 (paragraph / heading / section / ...)
        order_index : 형제 노드 간 정렬 인덱스 (0-based)
        title       : 노드 제목 (heading, section 등에서 사용)
        content     : 노드 본문 (텍스트)
        metadata    : 확장 key-value 구조 (JSONB)
        created_at  : 생성 시각 (버전 생성 시점과 동일)
    """

    id: str
    version_id: str
    node_type: str
    order_index: int
    metadata: dict[str, Any]
    created_at: datetime
    parent_id: Optional[str] = None
    title: Optional[str] = None
    content: Optional[str] = None
