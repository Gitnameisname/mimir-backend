"""
Schemas for Document Graph — S3 Phase 2 FG 2-4.

`GET /api/v1/documents/graph` 응답 스키마.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class GraphNodeOut(BaseModel):
    id: str = Field(..., description="document id 또는 tag:<uuid> / collection:<uuid>")
    type: Literal["document", "tag", "collection"]
    title: str
    document_type: Optional[str] = Field(
        None, description="type=document 일 때만"
    )


class GraphEdgeOut(BaseModel):
    source: str
    target: str
    type: Literal["backlink", "tagged", "in_collection"]


class GraphResponseOut(BaseModel):
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    truncated: bool = Field(
        False, description="documents 가 limit 에 걸려 잘렸을 때 true"
    )
    total_documents: int = Field(
        ..., description="viewer scope 안 documents 전체 수 (truncated 검증용)"
    )
