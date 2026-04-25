"""
Collections API request/response 스키마 — S3 Phase 2 FG 2-1.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CollectionCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)


class CollectionUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)

    def has_updates(self) -> bool:
        return self.name is not None or self.description is not None


class CollectionResponse(BaseModel):
    id: str
    owner_id: str
    name: str
    description: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    document_count: Optional[int] = Field(
        default=None,
        description="컬렉션에 포함된 문서 수 (미계산 시 null)",
    )

    model_config = {"from_attributes": True}


class CollectionAddDocumentsRequest(BaseModel):
    document_ids: list[str] = Field(..., min_length=1, max_length=200)


class CollectionAddDocumentsResponse(BaseModel):
    """컬렉션에 문서 추가 결과.

    - ``rejected`` 는 Scope 밖 또는 존재하지 않는 문서 수 (구체 id 는 유출 방지 목적으로 미반환).
    - ``inserted`` 는 실제 삽입된 건 수 (이미 들어있던 문서는 제외).
    """

    requested: int
    accepted: int
    inserted: int
    rejected: int
