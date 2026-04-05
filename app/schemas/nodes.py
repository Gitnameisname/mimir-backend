"""
Nodes API response Pydantic 스키마.

설계 원칙:
  - node 응답은 flat structured 방식 우선.
    (parent_id + order_index로 트리 구성 가능, 중첩 JSON 강제 안 함)
  - citation-friendly 식별자: id (UUID) + version_id + parent_id
  - 이후 트리 projection / AI RAG citation 확장을 막지 않음.
  - 현재 단계에서는 node 수정 endpoint 없음 (조회 전용).
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class NodeResponse(BaseModel):
    """노드 단건/목록 응답.

    flat structured 방식:
      - parent_id + order_index 로 클라이언트에서 트리 재구성 가능.
      - id 는 citation reference / deep-link 용도로 안정적으로 유지.
    """

    id: str = Field(description="노드 UUID (citation-friendly stable ID)")
    version_id: str = Field(description="소속 버전 UUID")
    parent_id: Optional[str] = Field(None, description="부모 노드 UUID (None = 최상위)")
    node_type: str = Field(description="노드 타입 (paragraph / heading / section / ...)")
    order_index: int = Field(description="형제 노드 간 정렬 인덱스 (0-based)")
    title: Optional[str] = None
    content: Optional[str] = None
    metadata: dict[str, Any] = Field(description="확장 메타데이터")
    created_at: datetime

    model_config = {"from_attributes": True}
