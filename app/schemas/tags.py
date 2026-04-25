"""
Tags API request/response 스키마 — S3 Phase 2 FG 2-2.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class TagResponse(BaseModel):
    id: str
    name: str = Field(description="정규화된 태그 이름 (NFKC + lower, 최대 64자)")
    created_at: datetime
    usage_count: Optional[int] = Field(
        default=None,
        description="이 태그가 붙은 문서 수 (검색/popular API 에서만 채워짐)",
    )

    model_config = {"from_attributes": True}


class DocumentTagResponse(BaseModel):
    """문서 상세의 태그 chip 렌더용."""

    id: str
    name: str
    source: str = Field(description="'inline' | 'frontmatter' | 'both'")
