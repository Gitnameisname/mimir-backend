"""
Schemas for User Search (Mention Typeahead) — S3 Phase 5 FG 5-3.

`GET /api/v1/users` 의 응답.

**보안 정책**: email / role_name / status / 기타 메타는 응답에 절대 포함하지 않는다 (R-A4).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class UserSearchItem(BaseModel):
    user_id: str = Field(..., description="멘션 영속화용 UUID")
    display_name: str


class UserSearchResponse(BaseModel):
    items: list[UserSearchItem]
    items_total: int = Field(..., description="응답에 포함된 항목 수")
    items_truncated: bool = Field(
        False, description="LIMIT 에 걸렸는지 여부 (사용자에게 더 좁은 prefix 안내용)"
    )
